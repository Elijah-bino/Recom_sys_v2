# RecomSys v2 on Azure Kubernetes (AKS)

Single-replica API with **SQLite** on a `ReadWriteOnce` PVC. Scale `replicas` above `1` only after moving to a shared database.

## Files

| Path | Purpose |
|------|---------|
| `deploy/docker/Dockerfile` | API container image |
| `deploy/k8s/namespace.yaml` | `recomsys` namespace |
| `deploy/k8s/pvc.yaml` | 20 Gi persistent disk for DB + matrix cache |
| `deploy/k8s/deployment.yaml` | Pod spec (edit image name) |
| `deploy/k8s/service.yaml` | `LoadBalancer` (Azure public IP) |
| `deploy/k8s/secret-*.example.yaml` | Examples for YouTube + JWT secrets |
| `ansible/playbook.yml` | Apply manifests from a machine with `kubectl` |

## Node / VM sizing (AKS node pool)

| Profile | Azure SKU | Nodes |
|---------|-----------|-------|
| Dev | `Standard_D4s_v5` (4 vCPU, 16 GiB) | 1–2 |
| Recommended | `Standard_D8s_v5` (8 vCPU, 32 GiB) | 2 |

## Quick deploy (Azure CLI)

```bash
az group create -n recomsys-rg -l eastus
az acr create -g recomsys-rg -n <youracr> --sku Basic
az aks create -g recomsys-rg -n recomsys-aks --node-count 2 --node-vm-size Standard_D8s_v5 --attach-acr <youracr>
az aks get-credentials -g recomsys-rg -n recomsys-aks --overwrite-existing
```

Build and push (from **repo root**):

```bash
az acr login -n <youracr>
az acr build -r <youracr> -t recomsys:v2 -f deploy/docker/Dockerfile .
```

Edit **`deploy/k8s/deployment.yaml`**: replace `YOUR_ACR.azurecr.io/recomsys:v2` with your image.

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/pvc.yaml
kubectl create secret generic recomsys-youtube -n recomsys --from-literal=YOUTUBE_API_KEY="$YOUTUBE_API_KEY" --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic recomsys-jwt -n recomsys --from-literal=JWT_SECRET="$(openssl rand -hex 32)" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl get svc recomsys-api -n recomsys -w
```

Open **`http://<EXTERNAL-IP>/ui`**.

## Ansible

```bash
cd ansible
ansible-playbook playbook.yml
```

Optional image roll:

```bash
ansible-playbook playbook.yml -e recomsys_image=<youracr>.azurecr.io/recomsys:v2
```

Requires **`kubectl`** and a valid **`KUBECONFIG`** (e.g. after `az aks get-credentials`).
