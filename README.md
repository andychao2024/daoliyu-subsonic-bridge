# 道理鱼 Subsonic API 桥

## 兼容  [Music-assistant](https://music-assistant.io/) OpenSubsonic Media Server Library 
[道理鱼音乐](https://www.daoliyu.com/) → 标准 [Subsonic](http://www.subsonic.org/) / [OpenSubsonic](https://opensubsonic.netlify.app/) API 桥接服务。

让 Navidrome / Feishin / SPlayer / 箭头音乐 / Symfonium 等支持 Subsonic 协议的客户端直接访问道理鱼的音乐库。

## 特性

| 功能 | 支持 |
|------|------|
| 歌曲/专辑/艺人浏览 | ✅ |
| 搜索 (search2/search3) | ✅ | 
| 歌单管理 (含封面) | ✅ |
| 随机歌曲 | ✅ |
| 按流派浏览 | ✅ |
| 歌曲播放 (Range/Seek) | ✅ |
| 收藏 (star/unstar) — 双向同步道理鱼 | ✅ |
| 封面获取 (al-/ar-/pl-/art_/trk_) | ✅ |
| 歌词 (LRC + 纯文本) | ✅ |
| 首页统计 (歌曲/专辑/艺人/文件夹数) | ✅ |
| 文件夹浏览 | ✅ |
| 文件流式传输 (含 Range 支持) | ✅ |
| A-Z 艺人索引 | ✅ |

## 快速开始

### 前提

- 飞牛私有云 / Linux 环境
- 已安装 [道理鱼音乐](https://www.daoliyu.com/) 并扫描完音乐库
- Python 3.8+

### 直接运行

```bash
# 克隆仓库
git clone https://github.com/YOUR_USER/daoliyu-subsonic-bridge.git
cd daoliyu-subsonic-bridge

# 运行（自动检测道理鱼数据库）
python3 daoliyu_subsonic_bridge.py --host 0.0.0.0 --port 4040
```

### 指定数据库

```bash
python3 daoliyu_subsonic_bridge.py --db /path/to/daoliyu.db
```

### 禁用鉴权（局域网使用）

```bash
python3 daoliyu_subsonic_bridge.py --no-auth
```

## Systemd 开机自启

```bash
sudo cp daoliyu-subsonic.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daoliyu-subsonic
```

查看状态:

```bash
systemctl status daoliyu-subsonic
journalctl -u daoliyu-subsonic -f
```

## 客户端连接

连接任意 Subsonic 客户端，指向:

```
服务器地址: http://你的NAS:4040
用户名:     道理鱼登录邮箱
密码:       道理鱼登录密码
```

### 推荐客户端

| 客户端 | 平台 | 说明 |
|--------|------|------|
| [箭头音乐 (Amcfy Music)](https://www.amcfy.com/) | iOS / Android | 功能完整，推荐 |
| [SPlayer](https://github.com/SPlayer-Dev/SPlayer) | 全平台 | 界面简洁 |
| [Feishin](https://github.com/jeffvli/feishin) | Desktop | 功能丰富 |
| [Symfonium](https://symfonium.app/) | Android | 高质量播放 |
| [音流](https://music.aqzscn.cn/) | iOS / Android | 多协议支持 |

## 认证方式

支持两种 Subsonic 认证方式:

1. **Token 认证**（推荐）: `?u=邮箱&t=md5(密码+盐)&s=盐`
2. **Hex 密码**（兼容）: `?u=邮箱&p=enc:hex(md5(密码))`

道理鱼用户的邮箱和密码就是登录凭证。

## 技术细节

### 架构

```
道理鱼 SQLite ──→ Python 桥 ──→ HTTP 4040 ──→ Subsonic API
```

- 单文件脚本，无外部依赖（仅用 Python 标准库）
- 多线程 HTTP 服务器（`ThreadingMixIn`）
- 5 分钟缓存刷新（`refresh_cache`）
- 对道理鱼数据库只读访问，不写任何数据（除 `star`/`unstar`）
- 支持 XML 和 JSON 两种响应格式

### 数据库适配

自动检测道理鱼的表结构（支持大小写/单复数差异）:

| 道理鱼表 | 用途 |
|----------|------|
| `tracks` | 歌曲元数据 |
| `albums` | 专辑信息 |
| `artists` | 艺人信息 |
| `track_artists` | 歌曲-艺人关联 |
| `playlists` / `playlist_tracks` | 歌单 |
| `favorite_tracks` / `favorite_albums` / `favorite_artists` | 收藏 |
| `users` | 用户认证 |

### 响应格式

- 完全对齐 OpenSubsonic 规范
- 额外包含 `funkwhaleVersion` / `type` 字段（兼容 Funkwhale 客户端）
- `isDir` / `isVideo` 返回布尔值（SPlayer 兼容）
- 歌词返回 `value` + `content` 双字段（箭头音乐 + SPlayer 兼容）
- `bitRate` 返回原始 bps 值

## FAQ

**Q: 更新道理鱼音乐后需要做什么？**
A: `systemctl restart daoliyu-subsonic` 即可（桥的缓存自动刷新）

**Q: 道理鱼更新后表结构变了怎么办？**
A: 桥自动检测表名和列名，大多数兼容。如有报错请联系维护者

**Q: 客户端提示"版本过低"？**
A: 桥报告 Subsonic API v1.16.0，绝大多数客户端兼容

**Q: 收藏不同步？**
A: 桥写入道理鱼的 `favorite_*` 表，道理鱼原生界面同步看到

## 文件清单

```
daoliyu_subsonic_bridge.py    # 主桥脚本 (~1190 行)
daoliyu-subsonic.service      # Systemd 服务文件
README.md                     # 本文件
```

## License

MIT
