# alchemiscale-k8s
An `alchemiscale` deployment for kubernetes.

This repo presents a fairly complete set of kubernetes ("k8s") resources that give a usable [`alchemiscale`](https://github.com/openforcefield/alchemiscale) deployment, featuring both server and compute components.

## deployment instructions

To deploy `alchemiscale` to a k8s cluster:

1. Set your desired secrets for the server components in [server/secrets.sh](server/secrets.sh).
   Deploy: `bash server/secrets.sh`.

2. Deploy namespace: `kubectl apply -f server/alchemiscale-namespace.yaml`.

3. Deploy configmap: `kubectl apply -f server/alchemiscale-configmap.yaml`.

4. Deploy neo4j: `kubectl apply -f server/neo4j-statefulset.yaml`.

5. Deploy client and compute APIs: `kubectl apply -f server/*api-deployment.yaml`

6. Deploy ingress: `kubectl apply -f server/alchemiscale-ingress.yaml`

7. Follow [instructions for creating user and compute identities](https://docs.alchemiscale.org/en/latest/operations.html#add-users); use `kubectl exec` instead of `docker run` for these calls.
You will need at least one user identity and one compute identity with permissions on at least one ``Scope`` to make use of ``alchemiscale``.

8. Set your desired settings for the compute services in [compute/synchronous-compute-settings.yaml](compute/synchronous-compute-settings.yaml).
   Deploy these as a secret: `bash compute/secrets.sh`.

9. Set desired number of replicas in [compute/compute-services.yaml](compute/compute-services.yaml).
   Deploy compute services: `kubectl apply -f compute/compute-services.yaml`
