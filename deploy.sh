#!/bin/bash
set -euo pipefail
PS4='[${BASH_SOURCE}:${LINENO}] '
set -x

echo "deploying backend..."

# Polls ECS describe-services to track deployment progress
# Args: $1=cluster, $2=service, $3=timeout_seconds
wait_for_deployment() {
    set +x  # Disable tracing inside this function
    local cluster="$1"
    local service="$2"
    local timeout="${3:-600}"
    local interval=10
    local elapsed=0

    echo "Waiting up to $((timeout/60)) minutes for deployment to complete..."

    while [ $elapsed -lt $timeout ]; do
        local response
        response=$(aws ecs describe-services \
            --cluster "$cluster" \
            --services "$service" \
            --query 'services[0]' \
            --output json 2>/dev/null)

        if [[ "${LOG_LEVEL:-}" == "DEBUG" ]]; then
            echo "$response" | jq .
        fi

        local deployments desired running rollout_state reason any_failed
        deployments=$(echo "$response" | jq '.deployments | length')
        desired=$(echo "$response" | jq '.desiredCount')
        running=$(echo "$response" | jq '.runningCount')
        rollout_state=$(echo "$response" | jq -r '.deployments[0].rolloutState // "UNKNOWN"')
        reason=$(echo "$response" | jq -r '.deployments[0].rolloutStateReason // "N/A"')

        # Check if ANY deployment failed (catches rollback scenarios)
        any_failed=$(echo "$response" | jq '[.deployments[].rolloutState] | any(. == "FAILED")')

        # Overwrite current line with status
        printf '\r\033[K[%d/%d s] Tasks: %s/%s | Deployments: %s | State: %s' \
            "$elapsed" "$timeout" "$running" "$desired" "$deployments" "$rollout_state"

        # Failure: circuit breaker triggered on any deployment
        if [ "$any_failed" = "true" ]; then
            reason=$(echo "$response" | jq -r '.deployments[] | select(.rolloutState == "FAILED") | .rolloutStateReason' | head -1)
            printf '\n'
            echo "Deployment FAILED (circuit breaker triggered): $reason"
            set -x
            return 1
        fi

        # Success: single deployment, all tasks running, rollout completed
        if [ "$deployments" -eq 1 ] && [ "$running" -eq "$desired" ] && [ "$rollout_state" = "COMPLETED" ]; then
            printf '\n'
            echo "Deployment successful!"
            set -x
            return 0
        fi

        sleep "$interval"
        elapsed=$((elapsed + interval))
    done

    printf '\n'
    echo "Timeout waiting for deployment after ${timeout}s"
    set -x
    return 124
}

#If there's a setEnv.sh script in the / directory, copy it and run it before starting
echo "Checking for setEnv script"
if [ -f "../build/setEnv.sh" ] ; then
    echo "Copy and run script setEnv.sh from deployment repo"
    cp "../build/setEnv.sh" .
    source setEnv.sh
elif [ -f "setEnv.sh" ] ; then
  source setEnv.sh
  echo "Using local setEnv.sh"
else
    echo "There is no script setEnv.sh"
fi

#Push backend contianers
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

#TODO convert all of this push logic to a function
BACKEND_IMAGE_NAME=python-backend
BACKEND_TAG=latest
BACKEND_REPO_URI=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$BACKEND_IMAGE_NAME:$BACKEND_TAG

HATCHET_WORKER_IMAGE_NAME=hatchet-worker
HATCHET_WORKER_TAG=latest
HATCHET_WORKER_REPO_URI=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$HATCHET_WORKER_IMAGE_NAME:$HATCHET_WORKER_TAG

SCIM_SERVER_IMAGE_NAME=scim-server
SCIM_SERVER_TAG=latest
SCIM_SERVER_REPO_URI=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$SCIM_SERVER_IMAGE_NAME:$SCIM_SERVER_TAG


DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --build-arg GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) -t $BACKEND_IMAGE_NAME -t $BACKEND_REPO_URI .
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --build-arg GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) -t $HATCHET_WORKER_IMAGE_NAME -t $HATCHET_WORKER_REPO_URI  -f content_services/Dockerfile .
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --build-arg GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) -t $SCIM_SERVER_IMAGE_NAME -t $SCIM_SERVER_REPO_URI -f scim/Dockerfile .

echo $BACKEND_REPO_URI
echo $HATCHET_WORKER_REPO_URI
echo $SCIM_SERVER_REPO_URI

BACKEND_LOCAL_DIGEST=$(docker image inspect $BACKEND_REPO_URI --format '{{json .Id}}' | grep -o 'sha256:[0-9a-f]\{64\}' )
HATCHET_WORKER_LOCAL_DIGEST=$(docker image inspect $HATCHET_WORKER_REPO_URI --format '{{json .Id}}' | grep -o 'sha256:[0-9a-f]\{64\}')

BACKEND_REMOTE_DIGEST=$(aws ecr describe-images \
  --repository-name "$BACKEND_IMAGE_NAME" \
  --image-ids imageTag="$BACKEND_TAG" \
  --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' \
  --output text 2>/dev/null || echo "NONE")

HATCHET_WORKER_REMOTE_DIGEST=$(aws ecr describe-images \
  --repository-name "$HATCHET_WORKER_IMAGE_NAME" \
  --image-ids imageTag="$HATCHET_WORKER_TAG" \
  --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' \
  --output text 2>/dev/null || echo "NONE")

if [ "$BACKEND_LOCAL_DIGEST" != "$BACKEND_REMOTE_DIGEST" ]; then
  echo "Backend Image has changed. Pushing new image..."
  docker push "$BACKEND_REPO_URI"
  export BACKEND_PUSHED=true
else
  echo "Image is up-to-date. No push needed."
  export BACKEND_PUSHED=false
fi

if [ "$HATCHET_WORKER_LOCAL_DIGEST" != "$HATCHET_WORKER_REMOTE_DIGEST" ]; then
  echo "Hatchet worker Image has changed. Pushing new image..."
  docker push "$HATCHET_WORKER_REPO_URI"
  export HATCHET_WORKER_PUSHED=true
else
  echo "Image is up-to-date. No push needed."
  export HATCHET_WORKER_PUSHED=false
fi

docker push "$SCIM_SERVER_REPO_URI"

uv sync --frozen --no-install-workspace

# Copy workspace lockfile to each lambda for UV-based bundling
echo "Copying uv.lock to lambda directories..."
for lambda_dir in lambdas/*/; do
    cp uv.lock "$lambda_dir"
done

set +e
npx cdk deploy --require-approval never
status=$?
set -e
# TODO allow pass through during "streaming updates" Other CLIs (PID=77666) are currently reading from cdk.out. Invoke the CLI in sequence, or use '--output' to synth into different directories."
if [[ $status -eq 1 ]]; then
    echo "CDK exited with status ${status}"
    exit 1
fi

#FORCE DEPLOY SECTION

CLUSTER_NAME=$(aws ecs list-clusters --query "clusterArns[?contains(@, 'V2BaseInfrastructureStack-BaseInfrastructureCoreInfrastructureCluster')]" --output text)

SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-ApiBackendBackendApiService')]" --output text)

HATCHET_BASE_WORKER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-HatchetBaselineWorkerHatchetWorkerSvc')]" --output text)

HATCHET_HEAVY_WORKER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-HatchetHeavyWorkerHatchetWorkerSvc')]" --output text)

HATCHET_ANALYTICS_WORKER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-HatchetAnalyticsWorkerHatchetWorkerSvc')]" --output text)

SCIM_SERVER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-SCIMServer')]" --output text)

if [ "$BACKEND_PUSHED" = "true" ]; then
    echo "Forcing backend redeploy..."

    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $SERVICE_NAME --force-new-deployment > /dev/null
fi

if [ "$HATCHET_WORKER_PUSHED" = "true" ]; then
    echo "Forcing hatchet worker redeploy..."

    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $HATCHET_BASE_WORKER_SERVICE_NAME --force-new-deployment > /dev/null
    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $HATCHET_HEAVY_WORKER_SERVICE_NAME --force-new-deployment > /dev/null
    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $HATCHET_ANALYTICS_WORKER_SERVICE_NAME --force-new-deployment > /dev/null
fi

echo "Forcing SCIM server redeploy..."
aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $SCIM_SERVER_SERVICE_NAME --force-new-deployment > /dev/null
#TODO Also wait for hatchet worker service to stablize?

echo "Waiting for ECS service to stabilize..."
if ! wait_for_deployment "$CLUSTER_NAME" "$SERVICE_NAME" 720; then
    exit 1
fi
echo "✅ Service became stable."
