#!/usr/bin/env bash
set -x
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "$0")/$(dirname "$(test -L "$0" && readlink "$0" || echo "/")")"; pwd)

arg1="${1-}"

BAZELISK_VERSION="v1.16.0"

platform="unknown"

case "${OSTYPE}" in
  msys)
    echo "Platform is Windows."
    platform="windows"
    # No installer for Windows
    ;;
  darwin*)
    echo "Platform is Mac OS X."
    platform="darwin"
    ;;
  linux*)
    echo "Platform is Linux (or WSL)."
    platform="linux"
    ;;
  *)
    echo "Unrecognized platform."
    exit 1
esac

architecture="${HOSTTYPE}"
echo "Architecture is $architecture"

if [ "${BAZEL_CONFIG_ONLY-}" != "1" ]; then
  # Sanity check: Verify we have symlinks where we expect them, or Bazel can produce weird "missing input file" errors.
  # This is most likely to occur on Windows, where symlinks are sometimes disabled by default.
  { git ls-files -s 2>/dev/null || true; } | (
    set +x
    missing_symlinks=()
    while read -r mode _ _ path; do
      if [ "${mode}" = 120000 ]; then
        test -L "${path}" || missing_symlinks+=("${path}")
      fi
    done
    if [ ! 0 -eq "${#missing_symlinks[@]}" ]; then
      echo "error: expected symlink: ${missing_symlinks[*]}" 1>&2
      echo "For a correct build, please run 'git config --local core.symlinks true' and re-run git checkout." 1>&2
      false
    fi
  )

  if [ "${OSTYPE}" = "msys" ]; then
    target="${MINGW_DIR-/usr}/bin/bazel.exe"
    mkdir -p "${target%/*}"
    curl -f -s -L -R -o "${target}" "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-linux-amd64"
  else
    # Buildkite mac instances
    if [[ -n "${BUILDKITE-}" ]] && [ "${platform}" = "darwin" ]; then
      mkdir -p "$HOME/bin"
      # Add bazel to the path.
      # shellcheck disable=SC2016
      printf '\nexport PATH="$HOME/bin:$PATH"\n' >> ~/.zshenv
      # shellcheck disable=SC1090
      source ~/.zshenv
      INSTALL_USER=1
    # Buildkite linux instance
    elif [ "${CI-}" = true ] || [ "${arg1-}" = "--system" ]; then
      INSTALL_USER=0
    # User
    else
      mkdir -p "$HOME/bin"
      INSTALL_USER=1
      export PATH=$PATH:"$HOME/bin"
    fi

    if [ "${architecture}" = "aarch64" ]; then
      # architecture is "aarch64", but the bazel tag is "arm64"
      url="https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-${platform}-arm64"
    elif [ "${architecture}" = "x86_64" ]; then
      url="https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-${platform}-amd64"
    fi

    if [ "$INSTALL_USER" = "1" ]; then
      target="$HOME/bin/bazel"
      curl -f -s -L -R -o "${target}" "${url}"
      chmod +x "${target}"
    else
      target="/bin/bazel"
      sudo curl -f -s -L -R -o "${target}" "${url}"
      sudo chmod +x "${target}"
    fi
  fi
fi

bazel --version

# clear bazelrc
echo > ~/.bazelrc

for bazel_cfg in ${BAZEL_CONFIG-}; do
  echo "build --config=${bazel_cfg}" >> ~/.bazelrc
done
if [ "${TRAVIS-}" = true ]; then
  echo "build --config=ci-travis" >> ~/.bazelrc

  # If we are in Travis, most of the compilation result will be cached.
  # This means we are I/O bounded. By default, Bazel set the number of concurrent
  # jobs to the the number cores on the machine, which are not efficient for
  # network bounded cache downloading workload. Therefore we increase the number
  # of jobs to 50
  # NOTE: Normally --jobs should be under 'build:ci-travis' in .bazelrc, but we put
  # it under 'build' here avoid conflicts with other --config options.
  echo "build --jobs=50" >> ~/.bazelrc
fi

if [ "$BUILDKITE" = "true" ]; then
  cp "${ROOT_DIR}"/../../.bazeliskrc ~/.bazeliskrc
fi

if [ "${GITHUB_ACTIONS-}" = true ]; then
  echo "build --config=ci-github" >> ~/.bazelrc
  echo "build --jobs="$(($(nproc)+2)) >> ~/.bazelrc
fi

if [ "${CI-}" = true ]; then

  # Ask bazel to anounounce the config it finds in bazelrcs, which makes
  # understanding how to reproduce bazel easier.
  echo "build --announce_rc" >> ~/.bazelrc

  echo "build --config=ci" >> ~/.bazelrc

  # In Windows CI we want to use this to avoid long path issue
  # https://docs.bazel.build/versions/main/windows.html#avoid-long-path-issues
  if [ "${OSTYPE}" = msys ]; then
    echo "startup --output_user_root=c:/tmp" >> ~/.bazelrc
  fi

  # If we are in master build, we can write to the cache as well.
  upload=0
  if [ "${TRAVIS_PULL_REQUEST-false}" = false ]; then
    # shellcheck disable=SC2154
    if [ -n "${BAZEL_CACHE_CREDENTIAL_B64:+x}" ]; then
      {
        printf "%s" "${BAZEL_CACHE_CREDENTIAL_B64}" | base64 -d - >> "${HOME}/bazel_cache_credential.json"
      } 2>&-  # avoid printing secrets
      upload=1
    elif [ -n "${encrypted_1c30b31fe1ee_key:+x}" ]; then
      {
        # shellcheck disable=SC2154
        openssl aes-256-cbc -K "${encrypted_1c30b31fe1ee_key}" \
            -iv "${encrypted_1c30b31fe1ee_iv}" \
            -in "${ROOT_DIR}/bazel_cache_credential.json.enc" \
            -out "${HOME}/bazel_cache_credential.json" -d
      } 2>&-  # avoid printing secrets
      # shellcheck disable=SC2181
      if [ 0 -eq $? ]; then
        upload=1
      fi
    fi
  fi
  if [ 0 -ne "${upload}" ]; then
    translated_path=~/bazel_cache_credential.json
    if [ "${OSTYPE}" = msys ]; then  # On Windows, we need path translation
      translated_path="$(cygpath -m -- "${translated_path}")"
    fi
    cat <<EOF >> ~/.bazelrc
build --google_credentials="${translated_path}"
EOF
  elif [ -n "${BUILDKITE-}" ]; then

    if [ "${platform}" = "darwin" ]; then
      echo "Using local disk cache on mac"
    cat <<EOF >> ~/.bazelrc
build --disk_cache=/tmp/bazel-cache
build --repository_cache=/tmp/bazel-repo-cache
EOF
    else
      echo "Using buildkite secret store to communicate with cache address"
      cat <<EOF >> ~/.bazelrc
build --remote_cache=${BUILDKITE_BAZEL_CACHE_URL}
EOF
      if [ "${BUILDKITE_PULL_REQUEST}" != "false" ]; then
        echo "build --remote_upload_local_results=false" >> ~/.bazelrc
      fi
    fi

  else
    echo "Using remote build cache in read-only mode." 1>&2
    cat <<EOF >> ~/.bazelrc
build --remote_upload_local_results=false
EOF
  fi
fi
