# Vodafone Balance

A small self-hosted service for checking the remaining credit shown in Vodafone Italy's authenticated **Fai da te** website. The browser and its profile run inside a container in K3s; no Python, Playwright, Chromium, or other modules need to be installed on the Linux host.

## Why browser automation?

Vodafone's consumer guidance lists the My Vodafone app and the authenticated Fai da te area for checking remaining credit. Its `contatori.vodafone.it` page is documented for data SIMs only. The open-source [PrepaidBalance](https://github.com/mueller-ma/PrepaidBalance) project parses USSD responses, which requires a device/provider path that is not available to this deployment.

This project automates the authenticated website instead. It does not bypass MFA or attempt to defeat Vodafone's access controls. You complete the login and SMS challenge yourself in the browser window. Vodafone may still expire the session and require login again.

## Current status / limitations

- First version / proof of concept; Vodafone may change the login or account page at any time.
- The balance parser intentionally fails rather than returning a random price if it cannot confidently find an amount near a credit-related label.
- The browser profile contains authentication cookies and must be treated as a secret. It is stored only on the Kubernetes PVC, never in Git.
- The application has HTTP Basic Authentication. Keep the Service private (ClusterIP) and access it through `kubectl port-forward` or a separately secured ingress.
- noVNC has no separate login. Do not expose port 6080 through an ingress or public LoadBalancer; use `kubectl port-forward` only.
- Do not use this as a financial source of truth until you have verified its result against Vodafone's own page.

## Build image

The GitHub Actions workflow builds and pushes multi-architecture images (`linux/amd64` and `linux/arm64`) to GHCR on pushes to `main`. If GHCR package visibility is private, configure an imagePullSecret in the namespace; public is simplest for a personal deployment because the image contains no credentials.

## Deploy to K3s

### 1. Create an application login secret

Choose a username and a strong password for this web UI. Do not commit these values to Git:

```sh
kubectl create namespace vodafone-balance
kubectl -n vodafone-balance create secret generic vodafone-balance-auth \
  --from-literal=username='choose-a-username' \
  --from-literal=password='replace-with-a-long-random-password'
```

If the namespace already exists, the first command may report `AlreadyExists`; continue with the Secret command. To update the Secret later, use `kubectl edit secret` or delete and recreate it.

### 2. Apply Kubernetes resources

Wait until the GitHub Actions build has completed and the GHCR `latest` image exists, then run:

```sh
kubectl apply -f deploy/kubernetes.yaml
kubectl -n vodafone-balance rollout status deployment/vodafone-balance
```

The deployment expects the image `ghcr.io/masterlog80/vodafone-balance:latest`. If GHCR is private, create an image pull secret and reference it in the Deployment before applying.

### 3. Open the web UI

In one terminal:

```sh
kubectl -n vodafone-balance port-forward service/vodafone-balance 8080:8080
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and enter the UI username/password created above.

### 4. Complete Vodafone login once

In another terminal:

```sh
kubectl -n vodafone-balance port-forward deployment/vodafone-balance 6080:6080
```

Open [http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale](http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale). If needed, click **Connect** in noVNC. In the browser window, choose **Privato**, sign in to Vodafone, and complete any SMS verification. Leave the browser open; the profile is saved on the PVC.

Return to the web UI and click **Check balance**. If Vodafone expires the session, repeat the manual login step. Avoid exposing noVNC to the internet.

### 5. API

With the same HTTP Basic Auth credentials:

```sh
curl -u 'choose-a-username:replace-with-a-long-random-password' \
  -X POST http://127.0.0.1:8080/api/balance
curl -u 'choose-a-username:replace-with-a-long-random-password' \
  http://127.0.0.1:8080/api/status
```

`POST /api/balance` navigates to Vodafone and tries to read the balance. A failure returns HTTP 502 and a human-readable error. `GET /api/status` returns the last successful reading and current error state.

## Troubleshooting

- **The UI says login may be required:** open noVNC, complete Vodafone login/MFA, then try again.
- **Could not confidently identify the residual credit:** inspect the page shown in noVNC. The Vodafone layout may have changed; do not trust a guessed amount.
- **ImagePullBackOff:** check the Actions build succeeded and that the GHCR package is readable by your cluster.
- **PVC pending:** check the default StorageClass in K3s.
- **Browser exits or the pod restarts:** inspect `kubectl -n vodafone-balance logs deployment/vodafone-balance`. The profile persists on the PVC, but browser processes themselves do not survive a pod restart.

## References

- [Vodafone: checking remaining credit](https://www.vodafone.it/privati/area-supporto/pagamenti-controllo-costi/controllo-costi-traffico/controllare-credito-residuo.html)
- [Vodafone: My Vodafone app features](https://www.vodafone.it/privati/area-supporto/applicazioni/myvodafone-app/cosa-posso-fare-dalla-myvodafone-app.html)
- [PrepaidBalance (USSD-based Android app)](https://github.com/mueller-ma/PrepaidBalance)
