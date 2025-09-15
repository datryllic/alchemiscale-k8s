#!/bin/bash

kubectl -n alchemiscale delete secret alchemiscale-neo4j-secrets 
kubectl -n alchemiscale delete secret alchemiscale-aws-secrets 
kubectl -n alchemiscale delete secret alchemiscale-jwt-secrets 

# neo4j
NEO4J_USER="neo4j"  # for Neo4j community edition, must be `neo4j`
NEO4J_PASS=""       # choose a password for your Neo4j instance
kubectl -n alchemiscale create secret generic alchemiscale-neo4j-secrets --from-literal="NEO4J_USER=$NEO4J_USER"\
                                                         --from-literal="NEO4J_PASS=$NEO4J_PASS"\
                                                         --from-literal="NEO4J_AUTH=$USER/$PASS"

# aws
## AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY only needed if API services are being deployed outside of AWS
## or on AWS resources without IAM role-based access to S3
AWS_ACCESS_KEY_ID=""
AWS_SECRET_ACCESS_KEY=""

## required for object store
AWS_S3_BUCKET=""       # name of bucket, not its ARN
AWS_S3_PREFIX=""       # prefix within the bucket to use, if desired
AWS_DEFAULT_REGION=""  # region the bucket resides in
kubectl -n alchemiscale create secret generic alchemiscale-aws-secrets --from-literal="AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" \
                                                       --from-literal="AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY" \
                                                       --from-literal="AWS_S3_BUCKET=$AWS_S3_BUCKET" \
                                                       --from-literal="AWS_S3_PREFIX=$AWS_S3_PREFIX" \
                                                       --from-literal="AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION"
# jwt
JWT_SECRET_KEY=""   # generate this with `python -c "import secrets; print(secrets.token_hex(32))"`
kubectl -n alchemiscale create secret generic alchemiscale-jwt-secrets --from-literal="JWT_SECRET_KEY=$JWT_SECRET_KEY"
