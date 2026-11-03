#!/usr/bin/env bash
# The client key: official mprice_decoder builds sign their Cozy Markets token registrations
# with it (mprice_decoder/sync.py), so the server hands tokens to mprice_decoder only, not to scripts.
#
#   ./clientkey.sh            # its public half, for the server's COZY_CLIENT_KEYS (deploy.sh sets it)
#   ./clientkey.sh --private  # its private half, as a build bundles it (release.sh)
#
# Either makes the key first if there's none. It lives on this PC only, next to the release key
# (~/.config/mprice_decoder/client-key.pem): back it up. Every release carries the private half, so a
# determined person can dig it out of the exe; the server's probation (Probation.java) is what keeps
# their data off the board. To retire a leaked key: move the file away, run this (a new key), deploy
# Cozy Markets, release mprice_decoder. Copies on the old key keep their tokens but can't get new ones.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
KEY_DIRECTORY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["key_directory"])' "$HERE/mprice_decoder/assets/compatibility.json")
KEY=${MPRICE_DECODER_CLIENT_KEY:-$HOME/.config/$KEY_DIRECTORY/client-key.pem}
if [[ ! -f $KEY ]]; then
  mkdir -p "$(dirname "$KEY")"
  (umask 077 && openssl genpkey -algorithm ed25519 -out "$KEY")
  echo "made a new client key: $KEY (back it up)" >&2
fi
case ${1:-} in
  "") openssl pkey -in "$KEY" -pubout -outform DER | tail -c 32 | base64 -w0 ;;
  --private) openssl pkey -in "$KEY" -outform DER | tail -c 32 | base64 -w0 ;;
  *) echo "usage: clientkey.sh [--private]" >&2; exit 1 ;;
esac
echo
