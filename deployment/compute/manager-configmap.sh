#!/bin/bash

kubectl -n alchemiscale delete configmap alchemiscale-k8s-manager-config

kubectl -n alchemiscale create configmap alchemiscale-k8s-manager-config --from-file=manager-settings.yaml --from-file=manager-job-spec.yaml
