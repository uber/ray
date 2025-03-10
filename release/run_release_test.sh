#!/bin/bash
# shellcheck disable=SC2317

set -e

if [ -n "$DEBUG" ]; then
  set -x
fi

cd "${0%/*}" || exit 1

reason() {
  # Keep in sync with e2e.py ExitCode enum
  if [ "$1" -eq 0 ]; then
    REASON="success"
  elif [ "$1" -ge 1 ] && [ "$1" -lt 10 ]; then
    REASON="runtime error"
  elif [ "$1" -ge 10 ] && [ "$1" -lt 20 ]; then
    REASON="infra error"
  elif [ "$1" -ge 30 ] && [ "$1" -lt 40 ]; then
    REASON="infra timeout"
  elif [ "$1" -eq 42 ]; then
    REASON="command timeout"
  elif [ "$1" -ge 40 ] && [ "$1" -lt 50 ]; then
    REASON="command error"
  fi
  echo "${REASON}"
}

RAY_TEST_SCRIPT=${RAY_TEST_SCRIPT-ray_release/scripts/run_release_test.py}
RAY_TEST_REPO=${RAY_TEST_REPO-https://github.com/ray-project/ray.git}
RAY_TEST_BRANCH=${RAY_TEST_BRANCH-master}
RELEASE_RESULTS_DIR=${RELEASE_RESULTS_DIR-/tmp/artifacts}
BUILDKITE_MAX_RETRIES=1
BUILDKITE_RETRY_CODE=79
BUILDKITE_TIME_LIMIT_FOR_RETRY=1800

# This is not a great idea if your OS is different to the one
# used in the product clusters. However, we need this in CI as reloading
# Ray within the python process does not work for protobuf changes.
INSTALL_MATCHING_RAY=${BUILDKITE-false}

export RAY_TEST_REPO RAY_TEST_BRANCH RELEASE_RESULTS_DIR BUILDKITE_MAX_RETRIES BUILDKITE_RETRY_CODE BUILDKITE_TIME_LIMIT_FOR_RETRY

if [ -z "${NO_INSTALL}" ]; then
  pip install --use-deprecated=legacy-resolver -q -r requirements.txt
  pip install -q -U boto3 botocore

  if [ "${INSTALL_MATCHING_RAY-false}" == "true" ]; then
    # Find ray-wheels parameter and install locally
    i=1
    for arg in "$@"; do
      j=$((i+1))
      if [ "$arg" == "--ray-wheels" ]; then
        PARSED_RAY_WHEELS="${!j}"
      fi
      i=$j
    done

    if [ -n "${PARSED_RAY_WHEELS}" ]; then
      echo "Installing Ray wheels locally: ${PARSED_RAY_WHEELS}"
      pip install -U --force-reinstall "${PARSED_RAY_WHEELS}"
    else
      echo "Warning: No Ray wheels found to install locally"
    fi
  fi
fi

if [ -z "${NO_CLONE}" ]; then
  TMPDIR=$(mktemp -d -t release-XXXXXXXXXX)
  echo "Cloning test repo ${RAY_TEST_REPO} branch ${RAY_TEST_BRANCH}"
  git clone -b "${RAY_TEST_BRANCH}" "${RAY_TEST_REPO}" "${TMPDIR}"
  pushd "${TMPDIR}/release" || true
  HEAD_COMMIT=$(git rev-parse HEAD)
  echo "The cloned test repo has head commit of ${HEAD_COMMIT}"

  # We only do this if RAY_TEST_REPO and RAY_TEST_BRANCH are pointing to ray master.
  # Theoretically, release manager may also run into this issue when manually triggering
  # release test runs. But cherry-picks are rare and thus it's less likely to run into
  # this racing condition, ignoring for now.
  if [ "${RAY_TEST_REPO}" == "https://github.com/ray-project/ray.git" ] && \
  [[ "${PARSED_RAY_WHEELS}" == *"master"*  ]] && \
  [ "${RAY_TEST_BRANCH-}" == "master" ] && [ -n "${RAY_COMMIT_OF_WHEEL-}" ] && \
  [ "${HEAD_COMMIT}" != "${RAY_COMMIT_OF_WHEEL}" ]; then
    echo "The checked out test code doesn't match with the installed wheel. \
This is likely due to a racing condition when a PR is landed between \
a wheel is installed and test code is checked out."
    echo "Hard resetting from ${HEAD_COMMIT} to ${RAY_COMMIT_OF_WHEEL}."
    git reset --hard "${RAY_COMMIT_OF_WHEEL}"
  fi
fi

if [ -z "${NO_INSTALL}" ]; then
  pip install --use-deprecated=legacy-resolver -c requirements.txt -e .
fi

RETRY_NUM=0
MAX_RETRIES=${MAX_RETRIES-1}

if [ "${BUILDKITE_RETRY_COUNT-0}" -ge 1 ]; then
  echo "This is a manually triggered retry from the Buildkite web UI, so we set the number of infra retries to 1."
  MAX_RETRIES=1
fi

ALL_EXIT_CODES=()
while [ "$RETRY_NUM" -lt "$MAX_RETRIES" ]; do
  RETRY_NUM=$((RETRY_NUM + 1))

  if [ "$RETRY_NUM" -gt 1 ]; then
    # Sleep for random time between 30 and 90 minutes
    SLEEP_TIME=$((1800 + RANDOM % 5400))

    if [ -n "${OVERRIDE_SLEEP_TIME}" ]; then
      SLEEP_TIME=${OVERRIDE_SLEEP_TIME}
    fi

    echo "----------------------------------------"
    echo "Retry count: ${RETRY_NUM}/${MAX_RETRIES}. Sleeping for ${SLEEP_TIME} seconds before retrying the run."
    echo "----------------------------------------"
    sleep "${SLEEP_TIME}"
  fi

  if [ -z "${NO_ARTIFACTS}" ]; then
    sudo rm -rf "${RELEASE_RESULTS_DIR}"/* || true
  fi

  _term() {
    echo "[SCRIPT $(date +'%Y-%m-%d %H:%M:%S'),...] Caught SIGTERM signal, sending SIGTERM to release test script"
    kill "$proc"
    wait "$proc"
  }

  START=$(date +%s)
  set +e

  trap _term SIGINT SIGTERM
  python "${RAY_TEST_SCRIPT}" "$@" &
  proc=$!

  wait "$proc"
  EXIT_CODE=$?

  set -e
  END=$(date +%s)

  REASON=$(reason "${EXIT_CODE}")
  RUNTIME=$((END-START))
  ALL_EXIT_CODES[${#ALL_EXIT_CODES[@]}]=$EXIT_CODE

  case ${EXIT_CODE} in
    0)
    echo "Script finished successfully on try ${RETRY_NUM}/${MAX_RETRIES}"
    break
    ;;
    30 | 31 | 32 | 33)
    echo "Script failed on try ${RETRY_NUM}/${MAX_RETRIES} with exit code ${EXIT_CODE} (${REASON})."
    ;;
    *)
    echo "Script failed on try ${RETRY_NUM}/${MAX_RETRIES} with exit code ${EXIT_CODE} (${REASON}), aborting."
    break
    ;;
  esac

done

if [ -z "${NO_ARTIFACTS}" ]; then
  sudo rm -rf /tmp/ray_release_test_artifacts/* || true
  sudo cp -rf "${RELEASE_RESULTS_DIR}"/* /tmp/ray_release_test_artifacts/ || true
fi

echo "----------------------------------------"
echo "Release test finished with final exit code ${EXIT_CODE} after ${RETRY_NUM}/${MAX_RETRIES} tries"
echo "Run results:"

COUNTER=1
for EX in "${ALL_EXIT_CODES[@]}"; do
  REASON=$(reason "${EX}")
  echo "  Run $COUNTER: Exit code = ${EX} (${REASON})"
  COUNTER=$((COUNTER + 1))
done

echo "----------------------------------------"

REASON=$(reason "${EXIT_CODE}")
echo "Final release test exit code is ${EXIT_CODE} (${REASON}). Took ${RUNTIME}s"

if [ "$EXIT_CODE" -eq 0 ]; then
  echo "RELEASE MANAGER: This test seems to have passed."
elif [ "$EXIT_CODE" -ge 30 ] && [ "$EXIT_CODE" -lt 40 ]; then
  echo "RELEASE MANAGER: This is likely an infra error that can be solved by RESTARTING this test."
else
  echo "RELEASE MANAGER: This could be an error in the test. Please REVIEW THE LOGS and ping the test owner."
fi

if [ -z "${NO_CLONE}" ]; then
  popd || true
  rm -rf "${TMPDIR}" || true
fi

if [[ ("$REASON" == "infra error" || "$REASON" == "infra timeout") && ("$RUNTIME" -le "$BUILDKITE_TIME_LIMIT_FOR_RETRY") ]]; then
  exit "$BUILDKITE_RETRY_CODE"
else
  exit "$EXIT_CODE"
fi
