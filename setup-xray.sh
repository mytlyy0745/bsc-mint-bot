#!/bin/bash
# Huawei Flexus L Xray VLESS+Reality+Vision 节点一键配置
# 密钥全部在服务器本地生成与派生，绝不靠屏幕抄写。
set -e
PORT=443
SNI="www.microsoft.com"

# 1. 生成 UUID
UUID=$(cat /proc/sys/kernel/random/uuid)

# 2. 生成密钥对，并从私钥派生公钥（派生值是唯一准绳）
xray x25519 > /tmp/keypair.txt
PRIV_RAW=$(grep -i private /tmp/keypair.txt | awk '{print $NF}')
xray x25519 -i "$PRIV_RAW" > /tmp/derived.txt
# -i 输出里第一行是钳制后的私钥，第二行是派生公钥；用 -i 的私钥写配置、用 -i 的公钥做 pbk
PRIV=$(grep -i private /tmp/derived.txt | awk '{print $NF}')
PUB=$(grep -i public /tmp/derived.txt | awk '{print $NF}')

# 3. 校验长度（私钥和公钥都应为 43 字符）
if [ ${#PRIV} -ne 43 ] || [ ${#PUB} -ne 43 ]; then
  echo "KEY_LEN_ERROR priv=${#PRIV} pub=${#PUB}"
  exit 1
fi

# 3.5 日志目录改属主为 xray（服务以 xray 用户运行，否则写日志会 permission denied）
mkdir -p /var/log/xray
chown -R xray:xray /var/log/xray
touch /var/log/xray.log
chown xray:xray /var/log/xray.log

# 4. 写 Xray 配置
cat > /usr/local/etc/xray/config.json << EOF
{
  "log": {
    "loglevel": "warning",
    "access": "/var/log/xray-access.log",
    "error": "/var/log/xray.log"
  },
  "inbounds": [
    {
      "port": $PORT,
      "protocol": "vless",
      "settings": {
        "clients": [
          {
            "id": "$UUID",
            "flow": "xtls-rprx-vision"
          }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "$SNI:443",
          "xver": 0,
          "serverNames": ["$SNI"],
          "privateKey": "$PRIV",
          "shortIds": [""]
        }
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls"]
      }
    }
  ],
  "outbounds": [
    {
      "protocol": "freedom",
      "tag": "direct"
    }
  ]
}
EOF

# 5. 校验配置并重启
xray -test -config /usr/local/etc/xray/config.json
systemctl restart xray
sleep 2
systemctl is-active xray || echo "XRAY_NOT_ACTIVE"
ss -tlnp | grep -w $PORT || netstat -tlnp 2>/dev/null | grep -w $PORT || echo "PORT_CHECK_MANUAL"

# 6. 输出客户端链接（保存到文件并打印）
LINK="vless://$UUID@111.91.16.126:$PORT?encryption=none&flow=xtls-rprx-vision&security=reality&sni=$SNI&fp=chrome&pbk=$PUB&sid=&type=tcp&headerType=none#华为新加坡"
echo "$LINK" > /root/bsc-mint-bot/huawei-node.txt
echo "UUID=$UUID"
echo "PUBKEY_LEN=${#PUB}"
echo "LINK_SAVED"
echo "$LINK"
