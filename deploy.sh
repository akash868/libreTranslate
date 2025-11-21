#!/usr/bin/env bash
set -euo pipefail

# -------- CONFIG: edit or export these before running ----------
AWS_REGION="${AWS_REGION:-ap-south-1}"            # change if needed
STACK_NAME="${STACK_NAME:-libretranslate-stack}"
REPO_NAME="${REPO_NAME:-libretranslate-lambda}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
DOCKERFILE="${DOCKERFILE:-Dockerfile.lambda}"
MODEL_TAR="${MODEL_TAR:-libretranslate-models.tar.gz}"  # ensure this file exists
MODEL_S3_KEY="${MODEL_S3_KEY:-models/models.tar.gz}"
# If you want to use an existing bucket, set EXISTING_BUCKET environment variable before running
EXISTING_BUCKET="${EXISTING_BUCKET:-}"
# ---------------------------------------------------------------

echo "Using region: $AWS_REGION"
aws configure set region "$AWS_REGION"

# get account id
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}"

echo "Account: $AWS_ACCOUNT_ID"
echo "ECR URI: $ECR_URI"

# 1) Build Docker image
echo "Building Docker image..."
docker build -t "${REPO_NAME}:${IMAGE_TAG}" -f "$DOCKERFILE" .

# 2) Create ECR repo if doesn't exist (idempotent)
echo "Ensuring ECR repository exists..."
aws ecr describe-repositories --repository-names "${REPO_NAME}" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "${REPO_NAME}"

# 3) Login to ECR and push image
echo "Logging into ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "Tagging and pushing image..."
docker tag "${REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}"
docker push "${ECR_URI}"

# 4) Prepare S3 bucket and upload models tarball
if [ -z "$EXISTING_BUCKET" ]; then
  # Bucket name will be created by CloudFormation by default (if left blank)
  # But CloudFormation needs to create the bucket; we will deploy stack first then upload.
  CREATE_BUCKET_LATER=true
  BUCKET_TO_USE=""
else
  CREATE_BUCKET_LATER=false
  BUCKET_TO_USE="$EXISTING_BUCKET"
  echo "Using existing bucket: $BUCKET_TO_USE"
  echo "Uploading models tar to s3://$BUCKET_TO_USE/$MODEL_S3_KEY"
  aws s3 cp "$MODEL_TAR" "s3://$BUCKET_TO_USE/$MODEL_S3_KEY"
fi

# 5) Deploy CloudFormation stack
echo "Deploying CloudFormation stack: $STACK_NAME"
if [ -z "$EXISTING_BUCKET" ]; then
  # pass empty ModelBucketName so CF creates one
  aws cloudformation deploy \
    --template-file deploy.yml \
    --stack-name "$STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides RepositoryName="$REPO_NAME" ImageTag="$IMAGE_TAG" ModelBucketName="" ModelObjectKey="$MODEL_S3_KEY"
else
  aws cloudformation deploy \
    --template-file deploy.yml \
    --stack-name "$STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides RepositoryName="$REPO_NAME" ImageTag="$IMAGE_TAG" ModelBucketName="$EXISTING_BUCKET" ModelObjectKey="$MODEL_S3_KEY"
fi

# 6) If CF created the bucket, find the bucket name from stack outputs and upload models
if [ "$CREATE_BUCKET_LATER" = true ]; then
  echo "CloudFormation should have created an S3 bucket. Fetching the bucket name from stack outputs..."
  # The template sets the ModelBucketUsed output (value is either created bucket logical name or provided name)
  BUCKET_TO_USE=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ModelBucketUsed'].OutputValue" --output text)
  if [ -z "$BUCKET_TO_USE" ] || [ "$BUCKET_TO_USE" = "None" ]; then
    echo "Failed to determine bucket name from stack outputs. Exiting."
    exit 1
  fi
  echo "Uploading models tar to s3://$BUCKET_TO_USE/$MODEL_S3_KEY"
  aws s3 cp "$MODEL_TAR" "s3://$BUCKET_TO_USE/$MODEL_S3_KEY"
fi

# 7) Create a Lambda Function URL (public, no auth) so you can test quickly
FUNCTION_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='LambdaFunctionName'].OutputValue" --output text)
echo "Function name from stack: $FUNCTION_NAME"

if [ -n "$FUNCTION_NAME" ]; then
  echo "Creating a public function URL (auth NONE) for quick testing..."
  aws lambda create-function-url-config --function-name "$FUNCTION_NAME" --auth-type NONE >/dev/null 2>&1 || \
    echo "Function URL likely already exists or couldn't be created. You can create one manually later."
  # get URL
  FUNC_URL=$(aws lambda get-function-url-config --function-name "$FUNCTION_NAME" --query "FunctionUrl" --output text || true)
  echo "Function URL: $FUNC_URL"
fi

echo "Deployment complete."
echo "If your app needs the models path, the function environment variables LT_S3_BUCKET and LT_S3_MODEL_KEY are set from the stack."
