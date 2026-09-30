#!/usr/bin/env bash
# 创建/复用本机自签代码签名证书，让 AKM 的 macOS 目录授权（桌面/文稿/下载）跨版本保留。
#
# 背景：scripts/build_app.sh 会优先用这里的证书签名。ad-hoc 签名（codesign --sign -）
# 的 designated requirement 是 cdhash，每次重建都被 macOS 当成「新 App」，于是目录
# 权限会反复弹窗；用同一张证书签名后，指定要求只绑定 bundle id 与证书，重建不再改变
# 身份，用户点过一次「允许」即长期有效。
#
# 幂等：证书已存在且被系统认可时只做校验；已有同名证书但未被认可时只补信任设置，
# 都不会重建——重建等于换身份，目录授权要再点一次。
#
# 证书与私钥默认放在 ~/Library/Application Support/AKM/signing（目录 0700、私钥 0600），
# 不在仓库内。请随 ~/.akm 一起备份：私钥丢失后只能重建证书，届时权限会再问一次。
set -euo pipefail

IDENTITY_NAME="${AKM_SIGN_IDENTITY_NAME:-AKM Local Signing}"
SIGN_DIR="${AKM_SIGNING_DIR:-$HOME/Library/Application Support/AKM/signing}"
KEYCHAIN="${AKM_SIGNING_KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"
P12_PASS="${AKM_SIGNING_P12_PASS:-akm-local}"

if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: 需要 openssl（macOS 自带 /usr/bin/openssl 即可）" >&2
  exit 1
fi

identity_line() {
  # 一次性取回身份列表再本地匹配，避免 `security | grep` 在 set -o pipefail 下被 SIGPIPE 影响
  local ids
  ids="$(security find-identity -v -p codesigning 2>/dev/null || true)"
  grep -F "\"$IDENTITY_NAME\"" <<<"$ids" || true
}

if [[ -n "$(identity_line)" ]]; then
  echo "签名身份已存在，直接复用（不重建，避免换身份导致目录授权再弹一次）："
  identity_line
  exit 0
fi

mkdir -p "$SIGN_DIR"
chmod 700 "$SIGN_DIR"

# 已有同名证书却查不到有效身份，通常是缺「代码签名」用途的信任设置：只补信任，不重建，
# 否则会留下两张同名证书，codesign 选哪张不稳定，目录授权反而会反复弹窗。
EXISTING_CERT="$(security find-certificate -c "$IDENTITY_NAME" -p "$KEYCHAIN" 2>/dev/null || true)"
if [[ -n "$EXISTING_CERT" ]]; then
  printf '%s\n' "$EXISTING_CERT" > "$SIGN_DIR/akm-local-signing.crt"
  chmod 644 "$SIGN_DIR/akm-local-signing.crt"
  security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$SIGN_DIR/akm-local-signing.crt"
  if [[ -n "$(identity_line)" ]]; then
    echo "已复用钥匙串中既有的同名证书（只补信任设置）："
    identity_line
    exit 0
  fi
  echo "ERROR: 钥匙串里已有名为「$IDENTITY_NAME」的证书，但没能变成有效签名身份（多半是私钥缺失）。" >&2
  echo "       请在「钥匙串访问」里处理旧证书后重试；脚本不会生成同名重复证书。" >&2
  exit 1
fi

# LibreSSL（macOS 自带 openssl）不支持 req -addext，扩展写进配置文件
cat > "$SIGN_DIR/ext.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3_codesign
prompt = no
[dn]
CN = AKM Local Signing
O = AKM
[v3_codesign]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

echo "生成证书与私钥：$SIGN_DIR"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
  -keyout "$SIGN_DIR/akm-local-signing.key" \
  -out "$SIGN_DIR/akm-local-signing.crt" \
  -config "$SIGN_DIR/ext.cnf"

openssl pkcs12 -export \
  -inkey "$SIGN_DIR/akm-local-signing.key" \
  -in "$SIGN_DIR/akm-local-signing.crt" \
  -out "$SIGN_DIR/akm-local-signing.p12" \
  -name "$IDENTITY_NAME" -passout "pass:$P12_PASS"
chmod 600 "$SIGN_DIR/akm-local-signing.key" "$SIGN_DIR/akm-local-signing.p12"

echo "导入钥匙串：$KEYCHAIN"
# -A 允许任意程序使用该私钥，换取 codesign 免交互；想更严格可改成
# -T /usr/bin/codesign，代价是首次签名会弹一次钥匙串确认。
security import "$SIGN_DIR/akm-local-signing.p12" -k "$KEYCHAIN" \
  -P "$P12_PASS" -T /usr/bin/codesign -T /usr/bin/security -A >/dev/null

# 自签证书默认不受信任，codesign 会判定身份无效，需加「代码签名」用途的用户级信任设置
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$SIGN_DIR/akm-local-signing.crt"

if [[ -z "$(identity_line)" ]]; then
  echo "ERROR: 导入后仍未出现有效签名身份，请检查钥匙串状态。" >&2
  exit 1
fi

echo
echo "完成，签名身份："
identity_line
echo
echo "scripts/build_app.sh 之后会自动使用该身份（也可用 AKM_SIGN_IDENTITY 指定其它身份）。"
echo "注意：换成新身份后，应用第一次启动会再弹一次目录授权，点「允许」即可；此后重建与自动更新都不再询问。"
