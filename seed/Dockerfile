# One-shot bucket seeder. Runs under the `bootstrap` compose profile only.
#
# minio/mc speaks the S3 API that rustfs implements, including the bucket
# policy call we need to make `geo-data` anonymously readable.
FROM minio/mc:latest

COPY entrypoint.sh /entrypoint.sh

# The base image sets `mc` as its entrypoint; we need a shell script instead.
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
