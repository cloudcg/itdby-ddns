# itdby-ddns

华为云 DNS 动态更新守护进程。容器周期性地探测本机公网 IP，与 `last_ip.txt` 记录的值比对，变化时通过华为云 DNS API 更新对应的 A 记录（支持 `*.example.com` 这类通配符记录）。

镜像内只包含代码，**不包含任何真实凭据**：访问密钥与域名列表都在运行时通过挂载的 `config/config.ini` 提供。

## 目录结构

```
Dockerfile                  构建定义，基于 python:3.9-slim
itdby_ddns.py               守护进程本体，单文件
requirements.txt            依赖：requests、huaweicloudsdkcore、huaweicloudsdkdns
config/config.ini.example   配置模板，复制成 config.ini 后填写
```

## 配置

真实配置不在仓库里。把 `config/config.ini.example` 复制为 `config/config.ini`，填入自己的值：

| 配置项 | 说明 |
| --- | --- |
| `credentials.ak` / `credentials.sk` | 华为云 IAM 访问密钥，在控制台「我的凭证 → 访问密钥」创建 |
| `dns.region` | 区域，例如 `af-south-1` |
| `dns.endpoint` | 可选，自定义 API 端点；`region` 与 `endpoint` 同时设置时以 `region` 为准，`endpoint` 不生效 |
| `dns.ttl` | 记录 TTL，默认 300 |
| `domains.domain_list` | JSON 数组，要维护的域名；通配符写 `*.example.com`，会作为通配符记录本身维护 |
| `log.log_dir` | 日志目录，默认 `./logs`，单文件 5MB 轮转、保留 2 份 |
| `task.interval_seconds` | 轮询间隔（秒），最小 30，默认 300 |
| `task.startup_check` | `true` 时每次启动强制与云端核对一次，避免重启后 DNS 过期 |

首次运行若缺少 `config/config.ini`，程序会生成一份模板并退出，提示填写后重跑。

## 构建

```bash
docker build -t itdby/ddns:v1.6 .
```

若网络拉不到 PyPI，把 `Dockerfile` 里安装依赖那行改成走镜像源：

```dockerfile
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
```

## 运行

`config/` 与 `logs/` 需要挂载出来，否则容器重建后配置和 IP 状态会丢失：

```bash
docker run -d --name itdby-hw-ddns --restart unless-stopped \
  -v "$PWD/config:/app/config" \
  -v "$PWD/logs:/app/logs" \
  itdby/ddns:v1.6
```

对应 compose 片段：

```yaml
services:
  ddns:
    image: itdby/ddns:v1.6
    container_name: itdby-hw-ddns
    restart: unless-stopped
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
    stop_grace_period: 10s
```

## 日志与行为

日志同时输出到 stdout 和轮转文件，`docker logs` 即可日常查看。IP 未变化时不产生输出，日志安静属正常；出现 `WARNING` 表示三个公网 IP 探测源（`icanhazip.com`、`ifconfig.me/ip`、`checkip.amazonaws.com`）全部失败。进程仅在收到 SIGTERM/SIGINT 时退出，退出前不会中断正在进行的更新。

更新顺序为：取公网 IP → 与 `config/last_ip.txt` 比较 → 变化时查询 Zone、读取 A 记录、按需创建或更新 → 成功后才原子写入 `last_ip.txt`。

## 构建上下文里没有的东西

`config.ini`、`last_ip.txt`、`logs/`、历史 `.bak` 副本都不在仓库中，`.gitignore` 已排除。真实密钥只应存在于部署机的 `config/config.ini`，不要提交回仓库。

## 许可

MIT，见 `LICENSE`。
