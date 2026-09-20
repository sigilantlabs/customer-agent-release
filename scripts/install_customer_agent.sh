#!/usr/bin/env bash
#
# The only host command a customer needs to run.  Release engineering replaces
# the public-key marker before publishing this file at
# https://get.sigilantlabs.com/install.
#
# The script deliberately downloads a signed image release and never clones
# this repository. Workload data is submitted only when --workload is supplied.
set -euo pipefail

readonly INSTALLER_VERSION="1"
readonly DEFAULT_CONTROL_URL="https://optimizer-api-production.up.railway.app"
readonly DEFAULT_RELEASE_URL="https://github.com/sigilantlabs/customer-agent-release/releases/latest/download/release.json"
readonly EXPECTED_RELEASE_SCOPE="__SIGILANT_RELEASE_SCOPE__"
readonly EXPECTED_RELEASE_SCHEMA="__SIGILANT_RELEASE_SCHEMA__"
readonly DEFAULT_IMAGE="ghcr.io/sigilantlabs/customer-agent:stable"
readonly DEFAULT_STATE_DIR="${SIGILANT_STATE_DIR:-${HOME:-/var/lib}/.local/share/sigilant-customer}"
# This marker is replaced in the public installer with the base64 encoded
# release signing key. It is intentionally not environment-overridable: the
# installer is the trust root and accepting a customer-provided key would
# defeat release verification.
readonly RELEASE_KEY_B64="__SIGILANT_RELEASE_PUBLIC_KEY_B64__"

CONTROL_URL="${SIGILANT_CONTROL_URL:-$DEFAULT_CONTROL_URL}"
RELEASE_URL="${SIGILANT_RELEASE_URL:-$DEFAULT_RELEASE_URL}"
ENROLLMENT_CODE="${SIGILANT_ENROLLMENT_CODE:-}"
STATE_DIR="$DEFAULT_STATE_DIR"
DRY_RUN=0
NO_SYSTEMD=0
WORKLOAD_PATH=""
PROVE_CREDENTIAL="${SIGILANT_LAUNCH_CREDENTIAL:-}"
# Containers use the invoking account's numeric identity.  This lets a
# cap-drop=ALL process access the private 0700 state directory without running
# as root or retaining setuid/setgid capabilities.  Honor sudo's caller fields
# only when the installer itself is root; an ordinary process must not be able
# to select uid 0 by exporting SUDO_UID.
INSTALL_UID="$(id -u)"
INSTALL_GID="$(id -g)"
if [[ "$INSTALL_UID" == "0" && "${SUDO_UID:-}" =~ ^[0-9]+$ && "${SUDO_GID:-}" =~ ^[0-9]+$ ]]; then
  INSTALL_UID="$SUDO_UID"
  INSTALL_GID="$SUDO_GID"
fi
readonly INSTALL_UID INSTALL_GID

# Docker accepts numeric identities which need not exist in the image's
# /etc/passwd.  Torch/vLLM still asks for a logical username while selecting
# compilation-cache paths, so provide a fixed non-secret identity and keep all
# writable caches on the customer-owned state mount.  These values are also
# applied explicitly by the installer so they cannot depend on a base image's
# user or HOME defaults.
declare -ar RUNTIME_ENV=(
  "HOME=/state"
  "USER=sigilant"
  "LOGNAME=sigilant"
  "HF_HOME=/state/models"
  "XDG_CACHE_HOME=/state/cache/xdg"
  "TORCHINDUCTOR_CACHE_DIR=/state/cache/torchinductor"
  "TRITON_CACHE_DIR=/state/cache/triton"
)
RUNTIME_ENV_ARGS=()
for runtime_env in "${RUNTIME_ENV[@]}"; do
  RUNTIME_ENV_ARGS+=(--env "$runtime_env")
done
readonly RUNTIME_ENV_ARGS
unset runtime_env

die() { printf 'Sigilant: %s\n' "$*" >&2; exit 2; }
info() { printf 'Sigilant: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Install and start the Sigilant customer GPU agent.

Usage: install_customer_agent.sh --enrollment-code CODE [--workload FILE --credential CREDENTIAL] [options]

Options:
  --enrollment-code CODE  one-time code from the Sigilant dashboard
  --control-url URL       Sigilant control plane (normally set by the signed release)
  --state-dir PATH        durable local state (default: ~/.local/share/sigilant-customer)
  --workload FILE         install the agent and immediately run Prove on this workload
  --credential VALUE      temporary API credential for Prove (Task 5 will replace this bridge)
  --no-systemd            run with Docker restart policy only
  --dry-run               show checks and actions without changing the host
  --help                  show this help

SIGILANT_RELEASE_URL may be set by release engineering or an enterprise
mirror. The image always comes from the signed release manifest.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enrollment-code)
      [[ $# -ge 2 && -n "$2" ]] || die "--enrollment-code needs a value"
      ENROLLMENT_CODE="$2"; shift 2 ;;
    --control-url)
      [[ $# -ge 2 && -n "$2" ]] || die "--control-url needs a value"
      CONTROL_URL="$2"; shift 2 ;;
    --state-dir)
      [[ $# -ge 2 && -n "$2" ]] || die "--state-dir needs a value"
      STATE_DIR="$2"; shift 2 ;;
    --workload)
      [[ $# -ge 2 && -n "$2" ]] || die "--workload needs a value"
      WORKLOAD_PATH="$2"; shift 2 ;;
    --credential)
      [[ $# -ge 2 && -n "$2" ]] || die "--credential needs a value"
      PROVE_CREDENTIAL="$2"; shift 2 ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option (use --help)" ;;
  esac
done

[[ -n "$ENROLLMENT_CODE" ]] || die "an enrollment code is required; copy it from the Sigilant dashboard"
[[ "$ENROLLMENT_CODE" != *$'\n'* && "$ENROLLMENT_CODE" != *$'\r'* ]] || die "the enrollment code contains invalid characters"
if [[ -n "$WORKLOAD_PATH" ]]; then
  [[ -n "$PROVE_CREDENTIAL" ]] || die "--credential is required when --workload is supplied"
elif [[ -n "$PROVE_CREDENTIAL" ]]; then
  die "--workload is required when --credential is supplied"
fi
[[ "$PROVE_CREDENTIAL" != *$'\n'* && "$PROVE_CREDENTIAL" != *$'\r'* ]] || die "the credential contains invalid characters"
[[ "$CONTROL_URL" =~ ^https://[^/?#[:space:]]+/?$ ]] || die "--control-url must be an HTTPS origin"
[[ "$RELEASE_URL" =~ ^https://[^/?#[:space:]]+(/[^?#[:space:]]*)?$ ]] || die "the release URL must be HTTPS"

if (( DRY_RUN )); then
  info "dry run: detected $(uname -s)/$(uname -m)"
  info "dry run: would inspect the GPU, install Docker/NVIDIA support if needed,"
  info "dry run: would pull a signed customer-agent image, enroll this host,"
  info "dry run: would install a restartable service and start the agent"
  [[ -z "$WORKLOAD_PATH" ]] || info "dry run: would run Prove on the supplied workload using this enrolled host"
  info "dry run: state directory: $STATE_DIR"
  exit 0
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  die "this release supports Linux GPU hosts; contact Sigilant for another host package"
fi
case "$(uname -m)" in
  x86_64|amd64) ;;
  *) die "this release supports Linux amd64 GPU hosts" ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is required; install it and run this same command again"
command -v openssl >/dev/null 2>&1 || die "OpenSSL is required to verify the signed release; install it and run this same command again"

as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then "$@"; else
    command -v sudo >/dev/null 2>&1 || die "administrator access is needed once to install the GPU container runtime"
    sudo "$@"
  fi
}

install_docker_if_missing() {
  if ! command -v docker >/dev/null 2>&1; then
    info "installing Docker automatically"
    if command -v apt-get >/dev/null 2>&1; then
      as_root apt-get update -qq
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io
      as_root systemctl enable --now docker || true
    elif command -v dnf >/dev/null 2>&1; then
      as_root dnf install -y docker
      as_root systemctl enable --now docker || true
    elif command -v yum >/dev/null 2>&1; then
      as_root yum install -y docker
      as_root systemctl enable --now docker || true
    else
      die "Docker is not installed and this Linux distribution has no supported package manager"
    fi
  fi
  command -v docker >/dev/null 2>&1 || die "Docker installation did not complete"
  docker info >/dev/null 2>&1 || {
    # A package install performed with sudo leaves the normal cloud user out
    # of the docker group.  Use sudo for this invocation; asking the customer
    # to log out and back in would break the one-command contract.
    if [[ "${EUID:-$(id -u)}" -ne 0 ]] && command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
      DOCKER_USE_SUDO=1
    else
      die "Docker is installed but its daemon is not available"
    fi
  }
}

DOCKER_USE_SUDO=0
docker_cmd() {
  if (( DOCKER_USE_SUDO )); then sudo docker "$@"; else docker "$@"; fi
}

install_nvidia_runtime_if_missing() {
  command -v nvidia-smi >/dev/null 2>&1 || die "no NVIDIA GPU driver was detected; use a GPU machine and run this same command again"
  nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1 || die "the NVIDIA driver cannot access the GPU"
  if ! docker_cmd info --format '{{json .Runtimes}}' 2>/dev/null | grep -q 'nvidia'; then
    info "installing NVIDIA container support automatically"
    if command -v apt-get >/dev/null 2>&1; then
      # Configure NVIDIA's signed package repository when the base cloud image
      # does not already provide it.  This is the documented repository, and
      # avoids asking the customer to run a second setup command.
      if ! apt-cache show nvidia-container-toolkit >/dev/null 2>&1; then
        command -v gpg >/dev/null 2>&1 || as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gnupg ca-certificates
        local keyring="/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
        local listfile="/etc/apt/sources.list.d/nvidia-container-toolkit.list"
        curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
          https://nvidia.github.io/libnvidia-container/gpgkey |
          as_root gpg --dearmor --yes -o "$keyring" ||
          die "could not configure NVIDIA's signed package repository"
        printf '%s\n' \
          "deb [signed-by=$keyring] https://nvidia.github.io/libnvidia-container/stable/deb/ /" |
          as_root tee "$listfile" >/dev/null ||
          die "could not configure NVIDIA's package repository"
      fi
      as_root apt-get update -qq
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-container-toolkit ||
        die "NVIDIA container support could not be installed automatically on this image"
    elif command -v dnf >/dev/null 2>&1; then
      as_root dnf install -y nvidia-container-toolkit || die "NVIDIA container support could not be installed automatically"
    else
      die "NVIDIA container support is missing; use a standard NVIDIA GPU image and run this same command again"
    fi
    command -v nvidia-ctk >/dev/null 2>&1 && as_root nvidia-ctk runtime configure --runtime=docker
    as_root systemctl restart docker || true
  fi
  docker_cmd run --rm --gpus all --entrypoint nvidia-smi nvidia/cuda:12.4.1-base-ubuntu22.04 \
    --query-gpu=name --format=csv,noheader >/dev/null 2>&1 ||
    die "the container runtime cannot access your GPU; the machine may not have a supported GPU"
}

json_value() {
  # Release JSON is generated by Sigilant with these scalar fields. Python is
  # preferred for correct JSON escaping; the sed fallback keeps the installer
  # usable on minimal cloud images without adding a host dependency.
  local key="$1" file="$2"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$key" "$file" <<'PY'
import json, sys
with open(sys.argv[2], encoding="utf-8") as f:
    value=json.load(f).get(sys.argv[1])
if not isinstance(value, str): raise SystemExit(1)
print(value)
PY
  else
    sed -nE 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$file" | head -n1
  fi
}

fetch_and_verify_release() {
  [[ "$RELEASE_KEY_B64" != *SIGILANT_RELEASE* ]] || die "this installer is not an official signed release"
  local dir manifest signature key verify
  dir="$(mktemp -d)"
  # `dir` is local to this function; an EXIT trap that expands it after the
  # function returns trips `set -u`. Keep cleanup state global and defensive.
  RELEASE_TMP_DIR="$dir"
  trap 'if [[ -n "${RELEASE_TMP_DIR:-}" ]]; then rm -rf "$RELEASE_TMP_DIR"; fi' EXIT
  manifest="$dir/release.json"; signature="$dir/release.sig"; key="$dir/release-key.pem"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$RELEASE_URL" -o "$manifest" || die "could not download the Sigilant release"
  local signature_url="${RELEASE_URL%.json}.sig"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$signature_url" -o "$signature" || die "could not download the Sigilant release signature"
  printf '%s' "$RELEASE_KEY_B64" | openssl base64 -d -A > "$key" || die "the signed release key is invalid"
  openssl pkeyutl -verify -pubin -inkey "$key" -rawin -in "$manifest" -sigfile "$signature" >/dev/null 2>&1 ||
    die "the Sigilant release signature is invalid; no image was pulled"
  local schema
  schema="$(json_value schema "$manifest")" || die "the signed release manifest is invalid"
  [[ "$schema" == "$EXPECTED_RELEASE_SCHEMA" ]] || die "the signed release schema is unsupported"
  local release_scope
  release_scope="$(json_value release_scope "$manifest")" || die "the signed release manifest is invalid"
  [[ "$release_scope" == "$EXPECTED_RELEASE_SCOPE" ]] || die "the signed release scope is not valid for this installer"
  RELEASE_ID="$(json_value release_id "$manifest")" || die "the signed release manifest is invalid"
  [[ "$RELEASE_ID" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "the signed release identifier is invalid"
  IMAGE="$(json_value image "$manifest")" || die "the signed release has no image"
  [[ "$IMAGE" =~ ^[a-zA-Z0-9./_-]+@sha256:[a-f0-9]{64}$ ]] || die "the signed release image identity is invalid"
  RELEASE_CONTROL_URL="$(json_value control_url "$manifest")" || RELEASE_CONTROL_URL="$CONTROL_URL"
  [[ "$RELEASE_CONTROL_URL" == "$CONTROL_URL" ]] || die "the release belongs to a different Sigilant control plane"
  info "verified Sigilant release $RELEASE_ID"
}

install_and_start() {
  local service_name="sigilant-customer-agent" image="$IMAGE"
  [[ "$STATE_DIR" != *$'\n'* && "$STATE_DIR" != *$'\r'* && "$STATE_DIR" != *,* ]] || die "--state-dir cannot contain commas or line breaks"
  local state_created=0
  if [[ ! -e "$STATE_DIR" ]]; then
    mkdir -p "$STATE_DIR"
    state_created=1
  fi
  STATE_DIR="$(cd -P -- "$STATE_DIR" && pwd)" || die "the state directory could not be resolved"
  if (( state_created )) && [[ "${EUID:-$(id -u)}" -eq 0 ]] && [[ "$INSTALL_UID:$INSTALL_GID" != "0:0" ]]; then
    chown "$INSTALL_UID:$INSTALL_GID" "$STATE_DIR" || die "could not assign the agent state directory to the invoking user"
  fi
  local state_owner
  state_owner="$(stat -c %u:%g "$STATE_DIR" 2>/dev/null || stat -f %u:%g "$STATE_DIR")" ||
    die "could not inspect the agent state directory"
  [[ "$state_owner" == "$INSTALL_UID:$INSTALL_GID" ]] ||
    die "the agent state directory belongs to another user; use that user's state directory"
  chmod 700 "$STATE_DIR"
  [[ "$(stat -c %a "$STATE_DIR" 2>/dev/null || stat -f %Lp "$STATE_DIR")" == "700" ]] || die "could not make the agent state directory private"
  local runtime_dir runtime_owner
  for runtime_dir in models cache cache/xdg cache/torchinductor cache/triton; do
    [[ ! -L "$STATE_DIR/$runtime_dir" ]] || die "the runtime cache path cannot be a symbolic link"
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
      install -d -m 700 -o "$INSTALL_UID" -g "$INSTALL_GID" -- "$STATE_DIR/$runtime_dir" ||
        die "could not prepare the customer-owned runtime cache"
    else
      install -d -m 700 -- "$STATE_DIR/$runtime_dir" ||
        die "could not prepare the customer-owned runtime cache"
    fi
    runtime_owner="$(stat -c %u:%g "$STATE_DIR/$runtime_dir" 2>/dev/null || stat -f %u:%g "$STATE_DIR/$runtime_dir")" ||
      die "could not inspect the runtime cache"
    [[ "$runtime_owner" == "$INSTALL_UID:$INSTALL_GID" ]] ||
      die "the runtime cache belongs to another user"
  done
  [[ ! -e "$STATE_DIR/host.json" || -s "$STATE_DIR/host-token" ]] ||
    die "the agent state is incomplete; remove the state directory or restore its host-token before retrying"
  docker_cmd pull "$image" >/dev/null || die "could not download the signed Sigilant runtime image"
  local expected_digest="${image##*@}" expected_ref="${image%%@*}@${image##*@}" actual_digests
  [[ "$expected_digest" =~ ^sha256:[a-f0-9]{64}$ ]] || die "the signed image digest is invalid"
  local -a provenance_env=("SIGILANT_CUSTOMER_IMAGE_DIGEST=$expected_digest")
  local -a provenance_env_args=(--env "${provenance_env[0]}")
  # Image ID is the config digest and is intentionally different from the OCI
  # manifest digest signed in release.json.  Verify RepoDigests instead.
  actual_digests="$(docker_cmd image inspect --format '{{join .RepoDigests "\n"}}' "$image" 2>/dev/null || true)"
  grep -F -x -q "$expected_ref" <<<"$actual_digests" ||
    die "the downloaded image does not match the signed release"
  # A repeated one-command invocation must not kill an active Prove leg. Reuse
  # the already-running agent only when its signed image, state mount, numeric
  # identity and control plane exactly match this invocation. Any other running
  # container fails closed and is left untouched.
  local existing_status existing_health existing_image existing_state existing_user existing_control existing_enrollment
  local existing_environment expected_environment runtime_environment_matches
  existing_status="$(docker_cmd inspect --format '{{.State.Status}}' "$service_name" 2>/dev/null || true)"
  if [[ "$existing_status" == "running" ]]; then
    # A missing health status means the signed image declares no Docker
    # HEALTHCHECK. A declared unhealthy status must fail closed: replacing or
    # reusing that container could interrupt work that still owns the GPU.
    existing_health="$(docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$service_name" 2>/dev/null || true)"
    [[ -z "$existing_health" || "$existing_health" == "healthy" ]] ||
      die "the existing Sigilant agent is unhealthy; it was left running to protect any active Prove job"
    existing_image="$(docker_cmd inspect --format '{{.Config.Image}}' "$service_name" 2>/dev/null || true)"
    existing_state="$(docker_cmd inspect --format '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}{{end}}{{end}}' "$service_name" 2>/dev/null || true)"
    existing_user="$(docker_cmd inspect --format '{{.Config.User}}' "$service_name" 2>/dev/null || true)"
    existing_control="$(docker_cmd inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$service_name" 2>/dev/null | sed -n 's/^SIGILANT_CONTROL_URL=//p' | head -n1)"
    existing_enrollment="$(docker_cmd inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$service_name" 2>/dev/null | grep -c '^SIGILANT_ENROLLMENT_CODE=' || true)"
    existing_environment="$(docker_cmd inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$service_name" 2>/dev/null || true)"
    runtime_environment_matches=1
    for expected_environment in "${RUNTIME_ENV[@]}"; do
      grep -F -x -q "$expected_environment" <<<"$existing_environment" || runtime_environment_matches=0
    done
    grep -F -x -q "${provenance_env[0]}" <<<"$existing_environment" || runtime_environment_matches=0
    if [[ "$existing_image" == "$image" && "$existing_state" == "$STATE_DIR" &&
          "$existing_user" == "$INSTALL_UID:$INSTALL_GID" && "$existing_control" == "$CONTROL_URL" &&
          "$existing_enrollment" == "0" && "$runtime_environment_matches" == "1" &&
          -s "$STATE_DIR/host-token" && -s "$STATE_DIR/host.json" ]]; then
      info "reusing the enrolled Sigilant agent already running on this machine"
      return 0
    fi
    die "an active Sigilant agent has different release or state settings; it was left running to protect any active Prove job"
  fi
  # Paused and restarting containers can still own a GPU child. Docker reports
  # exited/dead only after the container's processes are gone, so those are the
  # only existing states that are safe to replace automatically.
  if [[ -n "$existing_status" && "$existing_status" != "exited" && "$existing_status" != "dead" ]]; then
    die "the existing Sigilant agent is $existing_status; it was left untouched because active work cannot be ruled out"
  fi
  docker_cmd rm -f "$service_name" >/dev/null 2>&1 || true
  local -a enrollment_args=()
  if [[ ! -s "$STATE_DIR/host-token" && ! -s "$STATE_DIR/host.json" ]]; then
    enrollment_args+=(--env "SIGILANT_ENROLLMENT_CODE=$ENROLLMENT_CODE")
  fi
  docker_cmd run -d --name "$service_name" --restart unless-stopped --init --gpus all \
    --user "$INSTALL_UID:$INSTALL_GID" --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=2g \
    --shm-size=2g --env SIGILANT_CONTROL_URL="$CONTROL_URL" \
    "${RUNTIME_ENV_ARGS[@]}" \
    "${provenance_env_args[@]}" \
    "${enrollment_args[@]}" \
    --mount "type=bind,src=$STATE_DIR,dst=/state" "$image" >/dev/null ||
    die "the Sigilant agent could not start; no workload data was sent"
  # A running container only proves Docker accepted the process.  Wait for
  # enrollment to persist host identity, then recreate it without the one-time
  # code so it is absent from `docker inspect` and the long-lived environment.
  local ready=0 state status
  for _ in {1..30}; do
    status="$(docker_cmd inspect --format '{{.State.Status}}' "$service_name" 2>/dev/null || true)"
    if [[ "$status" != "running" ]]; then
      [[ "$status" == "" || "$status" == "created" ]] || {
        docker_cmd logs --tail 40 "$service_name" >&2 || true
        die "the Sigilant agent exited before enrollment completed"
      }
    elif [[ -s "$STATE_DIR/host-token" && -s "$STATE_DIR/host.json" ]]; then
      ready=1; break
    fi
    sleep 2
  done
  (( ready )) || {
    docker_cmd logs --tail 40 "$service_name" >&2 || true
    die "the Sigilant agent did not enroll; check the enrollment code and control-plane access"
  }
  if [[ -n "${enrollment_args[*]}" ]]; then
    docker_cmd rm -f "$service_name" >/dev/null || true
    docker_cmd run -d --name "$service_name" --restart unless-stopped --init --gpus all \
      --user "$INSTALL_UID:$INSTALL_GID" --cap-drop=ALL --security-opt=no-new-privileges --read-only \
      --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=2g \
      --shm-size=2g --env SIGILANT_CONTROL_URL="$CONTROL_URL" \
      "${RUNTIME_ENV_ARGS[@]}" \
      "${provenance_env_args[@]}" \
      --mount "type=bind,src=$STATE_DIR,dst=/state" "$image" >/dev/null ||
      die "the Sigilant agent could not restart after enrollment"
    local restarted=0
    for _ in {1..10}; do
      [[ "$(docker_cmd inspect --format '{{.State.Status}}' "$service_name" 2>/dev/null || true)" == "running" ]] &&
        restarted=1 && break
      sleep 1
    done
    (( restarted )) || {
      docker_cmd logs --tail 40 "$service_name" >&2 || true
      die "the Sigilant agent stopped after enrollment"
    }
  fi
  # The Docker restart policy is sufficient when Docker is accessed through
  # sudo. A user systemd unit would otherwise fail at reboot because it cannot
  # access the root-owned Docker socket.
  if (( NO_SYSTEMD == 0 && DOCKER_USE_SUDO == 0 )) && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    local docker_bin
    docker_bin="$(command -v docker)"
    mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    cat > "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/sigilant-customer-agent.service" <<EOF
[Unit]
Description=Sigilant customer GPU agent
After=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$docker_bin start $service_name
ExecStop=$docker_bin stop -t 30 $service_name
[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable sigilant-customer-agent.service >/dev/null
  fi
  info "agent is enrolled and running"
}

run_prove_if_requested() {
  [[ -n "$WORKLOAD_PATH" ]] || return 0
  command -v readlink >/dev/null 2>&1 || die "readlink is required to locate the workload file"
  local workload workload_extension mounted_workload result submission digest prove_status
  workload="$(readlink -f -- "$WORKLOAD_PATH")" || die "the workload path could not be resolved"
  [[ -f "$workload" ]] || die "the workload must be a readable JSON or JSONL file"
  [[ "$workload" != *$'\n'* && "$workload" != *$'\r'* && "$workload" != *,* ]] ||
    die "the workload path cannot contain commas or line breaks"
  case "$workload" in
    *.[Jj][Ss][Oo][Nn][Ll]) workload_extension="jsonl" ;;
    *.[Jj][Ss][Oo][Nn]) workload_extension="json" ;;
    *) die "the workload filename must end in .json or .jsonl" ;;
  esac
  # connected_prove uses the suffix to distinguish JSONL from JSON. Keep only
  # the normalized, allowlisted suffix in the container path; no part of the
  # customer-controlled filename is interpolated into the destination.
  mounted_workload="/input/workload.$workload_extension"
  digest="$(openssl dgst -sha256 "$workload")" || die "the workload could not be hashed"
  digest="${digest##*= }"
  [[ "$digest" =~ ^[a-f0-9]{64}$ ]] || die "the workload digest could not be determined"
  result="$STATE_DIR/prove-${digest:0:16}-result.json"
  submission="$STATE_DIR/prove-${digest:0:16}-submission.json"
  info "starting Prove on this machine's enrolled GPU"
  set +e
  printf '%s' "$PROVE_CREDENTIAL" | docker_cmd run --rm -i --init \
    --user "$INSTALL_UID:$INSTALL_GID" --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=64m \
    --env SIGILANT_CONTROL_URL="$CONTROL_URL" \
    "${RUNTIME_ENV_ARGS[@]}" \
    --mount "type=bind,src=$STATE_DIR,dst=/state" \
    --mount "type=bind,src=$workload,dst=$mounted_workload,readonly" \
    --entrypoint sigilant-connected-prove "$IMAGE" \
    --workload "$mounted_workload" --credential-stdin --host-record /state/host.json \
    --output "/state/${result##*/}" --submission-state "/state/${submission##*/}"
  prove_status="${PIPESTATUS[1]}"
  set -e
  [[ ! -f "$result" ]] || info "Prove result: $result"
  return "$prove_status"
}

install_docker_if_missing
install_nvidia_runtime_if_missing
fetch_and_verify_release
install_and_start
run_prove_if_requested
