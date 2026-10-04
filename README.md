# Fish Monitor

Raspberry Piへ接続したカメラから、同じLAN上のPCやスマートフォンへメダカのライブ映像を配信するアプリケーションです。

OV5647の640×480、GB10フレームをLinux V4L2 APIで取得し、OpenCVでデモザイクしてJPEGへ変換します。ブラウザーにはMJPEGとして配信します。

## 主な機能

- `/dev/video0`のmmapバッファからGB10フレームを取得
- `/dev/v4l-subdev0`へフレーム周期、露光、ゲインを設定
- 元画像をMJPEGとして配信
- 配信状態と実測fpsを表示
- カメラがない環境では合成画像で起動
- 任意のHTTP Basic認証

## Raspberry PiでのPodman実行

```bash
cp .env.example .env
podman build --tag fish-monitor:latest .
podman run --detach --replace \
  --name fish-monitor \
  --restart unless-stopped \
  --userns keep-id \
  --group-add keep-groups \
  --publish 8080:8080 \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --device /dev/video0 \
  --device /dev/v4l-subdev0 \
  --env-file .env \
  fish-monitor:latest
```

起動後、同じLAN上のブラウザーで`http://<Raspberry-PiのIPアドレス>:8080/`を開きます。

## カメラなしでの確認

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
CAMERA_BACKEND=synthetic python -m underwater_monitor
```

ブラウザーで`http://127.0.0.1:8080/`を開きます。

## 認証

`.env`で`WEB_USERNAME`と`WEB_PASSWORD`の両方を設定すると、HTTP Basic認証が有効になります。未設定の場合は認証なしでLANへ公開されます。

TLSは含まないため、インターネットへ直接公開しないでください。

## 主なHTTPエンドポイント

| パス | 用途 |
|---|---|
| `/` | 監視画面 |
| `/stream.mjpg?mode=original` | ライブ映像 |
| `/snapshot.jpg?mode=original` | 最新のJPEG画像 |
| `/api/status` | 取得状態と実測fps |
| `/healthz` | プロセスの生存確認 |
| `/readyz` | 新しいフレームを取得できているか確認 |

## 使用ライブラリ

- [Pillow](https://python-pillow.github.io/) — MIT-CMU
- [NumPy](https://numpy.org/) — BSD-3-Clauseおよび同梱コンポーネントのライセンス
- [OpenCV](https://opencv.org/) — Apache-2.0および同梱コンポーネントのライセンス

このリポジトリはライブラリ本体を同梱せず、`requirements.txt`またはUbuntuパッケージからインストールします。

## License

MIT Licenseです。ライセンス本文はリポジトリルートの`LICENSE`を参照してください。
