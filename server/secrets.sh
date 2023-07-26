#!/bin/bash

kubectl -n alchemiscale delete secret alchemiscale-neo4j-secrets 
kubectl -n alchemiscale delete secret alchemiscale-aws-secrets 
kubectl -n alchemiscale delete secret alchemiscale-jwt-secrets 

# neo4j
USER=""
PASS=""
kubectl -n alchemiscale create secret generic alchemiscale-neo4j-secrets --from-literal="NEO4J_USER=$USER"\
                                                         --from-literal="NEO4J_PASS=$PASS"\
                                                         --from-literal="NEO4J_AUTH=$USER/$PASS"

# aws
kubectl -n alchemiscale create secret generic alchemiscale-aws-secrets --from-literal="AWS_ACCESS_KEY_ID=" \
                                                       --from-literal="AWS_SECRET_ACCESS_KEY=" \
                                                       --from-literal="AWS_S3_BUCKET=" \
                                                       --from-literal="AWS_S3_PREFIX=" \
                                                       --from-literal="AWS_DEFAULT_REGION="
# jwt
kubectl -n alchemiscale create secret generic alchemiscale-jwt-secrets --from-literal="JWT_SECRET_KEY="
