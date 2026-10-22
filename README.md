# Vodafone Balance

A self-hosted dashboard for checking the remaining credit on a Vodafone Italy prepaid SIM through the authenticated **Fai da te** website. The browser, its persistent profile, and all application dependencies run inside a container.

![Vodafone Balance dashboard](docs/screenshot-dashboard.svg)

## Features

- Dark responsive UI in the same style as my other self-hosted dashboards.
- Shows the **app name, startup time, application version and live Kubernetes image name:tag** in the bottom bar.
- Reads the running image from the current Pod object using a dedicated ServiceAccount with only `get` permission on that Pod.
- Manual Vodafone login and SMS MFA; the browser profile persists on a PVC.
- HTTP Basic Authentication on the UI and API.
- JSON API for balance/status and a deploy script.
- Multi-architecture container image for `linux/amd64` and `linux/arm64`.

## Screenshots

### Main dashboard

![Dashboard with balance, runtime information and image footer](docs/screenshot-dashboard.svg)

### First login / SMS verification

![Instructions for completing Vodafone authentication in the persistent browser](docs/screenshot-login.svg)

Screenshots are illustrative UI previews; the displayed balance and runtime values are sample data, not a live Vodafone account.

## Deploy to K3s

The included manifest deploys everything in the `default` namespace. It creates the ServiceAccount, namespace-scoped Role/RoleBinding, PVC, Deployment and ClusterIP Service. The application ServiceAccount has read-only `get` access to Pod objects in `default` (no list/watch access, Secrets, or other resource types). The app only requests its own Pod by name to display the image actually running.

### 1. Deploy using the script

Run from a clone of this repository. The script prompts for UI credentials (password input is hidden), creates/updates the Kubernetes Secret without saving credentials in Git, applies the manifest, and waits for rollout:

```sh
git clone https://github.com/masterlog80/vodafone-balance.git
cd vodafone-balance
chmod +x deploy.sh
./deploy.sh
```

You can pass credentials non-interactively, for example from a secret manager or CI:

```sh
APP_USERNAME='my-user' APP_PASSWORD='use-a-long-random-password' ./deploy.sh
```

The script needs `kubectl` configured for your K3s cluster. The application itself needs no Python, Playwright, Chromium or other application modules installed on the host.

### 2. Open the UI

In one terminal:

```sh
kubectl -n default port-forward service/vodafone-balance 8080:8080
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and enter the UI credentials you set with the script.

### 3. Complete Vodafone login

In a second terminal:

```sh
kubectl -n default port-forward deployment/vodafone-balance 6080:6080
```

Open [http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale](http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale). In the remote browser, select **Privato**, sign in and complete SMS verification. Return to the dashboard and click **Check balance**.

![Login guidance](docs/screenshot-login.svg)

**Security:** noVNC has no separate authentication. Keep port 6080 private and use `kubectl port-forward` only; never expose it through an ingress or public LoadBalancer. The saved browser profile contains session cookies.

## Docker Compose

For a local Docker run (the ports bind to loopback only):

```sh
export APP_USERNAME='my-user'
export APP_PASSWORD='use-a-long-random-password'
docker compose up -d
```

The profile is stored in a named Docker volume. Open the UI at `http://127.0.0.1:8080`; the noVNC browser is at `http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale`.

## API

The same HTTP Basic Auth credentials protect the API:

```sh
curl -u "$APP_USERNAME:$APP_PASSWORD" -X POST http://127.0.0.1:8080/api/balance
curl -u "$APP_USERNAME:$APP_PASSWORD" http://127.0.0.1:8080/api/status
curl -u "$APP_USERNAME:$APP_PASSWORD" http://127.0.0.1:8080/api/info
```

- `POST /api/balance` attempts to read the current balance and returns JSON.
- `GET /api/status` returns the last reading, error state and runtime metadata.
- `GET /api/info` returns the app name/version, process startup time, namespace and running image.
- `GET /healthz` is an unauthenticated Kubernetes health probe.

## Build and publish

GitHub Actions builds and publishes `ghcr.io/masterlog80/vodafone-balance:latest` and a commit-SHA tag for both AMD64 and ARM64 when `main` changes. The image must be readable by your cluster. If you make the GHCR package private, configure an image pull secret.

## Troubleshooting

- **Login required:** open noVNC, complete Vodafone login/SMS verification, then retry.
- **Balance not detected:** inspect the authenticated page in noVNC. The parser fails rather than guessing if the page changes.
- **Image displays fallback value:** check the ServiceAccount/Role/RoleBinding and the pod logs. The RBAC permission is intentionally limited to reading the app's own Pod.
- **ImagePullBackOff:** verify the GitHub Actions run and GHCR package visibility.
- **PVC pending:** check the default StorageClass in K3s.
- **Session expired:** sign in again through noVNC; Vodafone may periodically expire sessions.

## References

- [Vodafone: checking remaining credit](https://www.vodafone.it/privati/area-supporto/pagamenti-controllo-costi/controllo-costi-traffico/controllare-credito-residuo.html)
- [Vodafone: My Vodafone app features](https://www.vodafone.it/privati/area-supporto/applicazioni/myvodafone-app/cosa-posso-fare-dalla-myvodafone-app.html)
- [PrepaidBalance (USSD-based Android app)](https://github.com/mueller-ma/PrepaidBalance)
