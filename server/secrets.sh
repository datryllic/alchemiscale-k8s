#!/bin/bash

kubectl delete secret neo4j-secrets 
kubectl delete secret aws-secrets 
kubectl delete secret jwt-secrets 

# neo4j
USER=""
PASS=""
kubectl create secret generic neo4j-secrets --from-literal="NEO4J_USER=$USER"\
                                            --from-literal="NEO4J_PASS=$PASS"\
                                            --from-literal="NEO4J_AUTH=$USER/$PASS"

# aws
kubectl create secret generic aws-secrets --from-literal="AWS_ACCESS_KEY_ID=" \
                                          --from-literal="AWS_SECRET_ACCESS_KEY=" \
                                          --from-literal="AWS_S3_BUCKET=" \
                                          --from-literal="AWS_S3_PREFIX=" \
                                          --from-literal="AWS_DEFAULT_REGION="
# jwt
kubectl create secret generic jwt-secrets --from-literal="JWT_SECRET_KEY="
