# scripts/ensure_models_on_efs.py
import os
import io
import tarfile
import logging
import boto3

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def stream_extract_s3_to_dir(bucket, key, target_dir):
	"""
	Stream a tar.gz object from S3 and extract into target_dir safely.
	"""
	s3 = boto3.client('s3')
	obj = s3.get_object(Bucket=bucket, Key=key)
	body = obj['Body']

	# Wrap streaming body into a file-like object that tarfile can read
	stream = io.BufferedReader(body)

	with tarfile.open(fileobj=stream, mode='r|gz') as tar:
		for member in tar:
			# Security: prevent extraction outside target_dir
			member_path = os.path.join(target_dir, member.name)
			if not os.path.commonpath([os.path.abspath(target_dir)]) == os.path.commonpath([os.path.abspath(target_dir), os.path.abspath(member_path)]):
				logger.warning(f"Skipping suspicious member: {member.name}")
				continue
			try:
				tar.extract(member, path=target_dir)
			except Exception:
				logger.exception(f"Failed to extract {member.name}")
	logger.info("Streaming extract complete")
	return True


def ensure_models(mount_path='/mnt/models'):
	bucket = os.environ.get('LT_S3_BUCKET')
	key = os.environ.get('LT_S3_MODEL_KEY')
	if not bucket or not key:
		logger.error('LT_S3_BUCKET or LT_S3_MODEL_KEY not set')
		return False

	# if mount not present, create but warn
	if not os.path.exists(mount_path):
		logger.info(f"Mount path {mount_path} not found; creating")
		os.makedirs(mount_path, exist_ok=True)

	# if directory not empty, assume populated
	if any(os.scandir(mount_path)):
		logger.info('Models present in mount; skipping download')
		return True

	try:
		stream_extract_s3_to_dir(bucket, key, mount_path)
	except Exception:
		logger.exception('Failed to download/extract models')
		return False

	return True


if __name__ == '__main__':
	ok = ensure_models()
	exit(0 if ok else 2)