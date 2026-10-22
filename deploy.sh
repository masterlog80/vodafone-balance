#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="default"
MANIFEST="${MANIFEST:-deploy/kubernetes.yaml}"
SECRET_NAME="vodafone-balance-auth"

command -v kubectl >/dev/null || { echo "ERROR: kubectl is required." >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 1; }

if [[ -z "${APP_USERNAME:-}" ]]; then read -r -p "Web UI username: " APP_USERNAME; fi
if [[ -z "${APP_PASSWORD:-}" ]]; then
  read -r -s -p "Web UI password: " APP_PASSWORD
  echo
fi
[[ -n "$APP_USERNAME" && -n "$APP_PASSWORD" ]] || { echo "ERROR: username/password cannot be empty." >&2; exit 1; }

echo "Applying Vodafone Balance resources in namespace '$NAMESPACE'..."
# Manifest is designed for default. For an alternate namespace, use kubectl's
# namespace override and ensure the Secret is created in that same namespace.
kubectl create secret generic "$SECRET_NAME" -n "$NAMESPACE" \
  --from-literal=username="$APP_USERNAME" \
  --from-literal=password="$APP_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -n "$NAMESPACE" -f "$MANIFEST"
kubectl rollout status deployment/vodafone-balance -n "$NAMESPACE" --timeout=300s

echo
echo "Deployment is ready."
echo "UI:     kubectl port-forward -n $NAMESPACE service/vodafone-balance 8080:8080"
echo "noVNC:  kubectl port-forward -n $NAMESPACE deployment/vodafone-balance 6080:6080"
echo "Then open http://127.0.0.1:8080 and http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
