#!/bin/bash

kubectl -n alchemiscale delete secret alchemiscale-compute-settings-yaml

kubectl -n alchemiscale create secret generic alchemiscale-compute-settings-yaml --from-file=synchronous-compute-settings.yaml
