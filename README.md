# 防 AI 互聊（BotGuard）

> AstrBot 插件：自动识别群里的其他机器人 / AI，阻止两个 AI 无限对聊。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.0.0-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Platform](https://img.shields.io/badge/platform-aiocqhttp-orange)](https://github.com/AstrBotDevs/AstrBot)

## 它解决的是什么

AstrBot 自带的 `ignore_bot_self_message` 只过滤**自己发的消息**，对其他机器人没有任何防护。而唤醒检查里，**被 @** 和 **被引用**都会触发 LLM 请求。

于是两个都挂着大模型的 QQ 号会这样滚起来：

```
你 → @A：你好
A  → @你：你好呀（你还想聊点什么）
你 → @A：我都行，你呢
A  → @你：我也是……
（无限循环，刷屏 + 烧 token）
```

## 三层防护

| 层 | 时机 | 做什么 |
|---|---|---|
| **收** | LLM 请求发出前 | 认出是别的 bot → 直接掐断，**不烧 token、不发消息** |
| **发** | 回复发送前 | 删掉指向对方的 `@` 和引用 → 对方没被叫醒，链自然断 |
| **熔断** | 每轮对话 | 同一个 bot 连续来回超过 N 轮 → 静默一段时间 |

三者的关系：第 2 层让大多数情况**根本聊不起来**；万一对方是靠关键词 / 自动回复触发的（不依赖 @），第 3 层兜底；第 1 层负责在认出来之后零成本止损。

> 效果提示：如果对方是「被 @ 才回」的 bot，装上后通常**只回 1 轮就断了**——因为你的回复里已经没有 @ 它了。这是预期行为，不是故障。

## 识别「其他 AI」的三种依据

1. **静态名单** `bot_ids`：最准，配置里直接填 QQ 号（默认留空，需自己填）。
2. **平台标记**：原始事件里带 `is_robot` / `isRobot` 等字段时自动命中。
3. **行为学习**：本 bot 每次发言后，第一个接话的账号如果累计多次「秒回」（默认 4 次、15 秒内），判定为 AI 并记住，重启不丢。

误判了？`/botguard rm QQ号` 一键移除。

## 什么时候会恢复

**真人一说话，对话链立刻清零、静默立刻解除。** 所以正常问答完全不受影响——只有「两个 AI 你来我往」才会被掐。管理员账号也永不判定成 bot。

## 安装

### 方式一：从 GitHub 链接安装（推荐）

AstrBot 面板 → 插件 → 插件市场 → 右下角 **「+」** → 从链接安装，填：

```
https://github.com/<你的GitHub用户名>/<仓库名>
```

### 方式二：上传 zip

面板 → 插件 → 插件市场 → 右下角 **「+」** → 从文件安装，上传 `astrbot_plugin_botguard.zip`。

### 方式三：手动

把仓库里的 `main.py`、`metadata.yaml`、`_conf_schema.json` 放进：

```
<AstrBot数据目录>/plugins/astrbot_plugin_botguard/
```

然后重启 AstrBot。

> 安装完成后请**重启一次 AstrBot**，新插件才会加载。

## 配置

面板 → 插件 → 防 AI 互聊 → 齿轮图标。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `bot_ids` | 空 | 其他机器人的 QQ 号，逗号/空格/换行分隔 |
| `auto_learn` | 开 | 自动学习未知 bot |
| `auto_learn_hits` | 4 | 连续几次秒回才判定 |
| `auto_learn_window` | 15 | 多少秒内接话算「秒回」 |
| `auto_learn_expire` | 1800 | 学习记录多久算过期 |
| `max_rounds` | 3 | 最多允许来回几轮 |
| `chain_window` | 120 | 间隔超过这么久算新一轮 |
| `cooldown_seconds` | 600 | 熔断后静默多久（真人说话立即解除） |
| `reset_on_human` | 开 | 真人说话立即恢复对话链 |
| `strip_mention` | 开 | 发送时去掉指向 bot 的 @ / 引用 |
| `inject_hint` | 开 | 放行时提醒模型「对方是 AI，简短回」 |
| `notify_on_block` | 关 | 熔断时在群里说一句 |
| `only_groups` | 空 | 只在这些群生效，留空＝全部 |

## 指令（需管理员）

```
/botguard            查看状态
/botguard list       看名单
/botguard add 123456 手动加入
/botguard rm 123456  移除（误判就用这个）
/botguard reset      清空对话链与学习统计
/botguard off        临时暂停（重启后恢复配置值）
/botguard on         恢复
```

别名：`/防AI`、`/bg`。

## 排障

| 现象 | 先做什么 |
|---|---|
| 还是互聊 | `/botguard status` 看对方 QQ 在不在名单；不在就 `add` 进去 |
| 把真人当成 bot 了 | `/botguard rm QQ号`，并把 `auto_learn_hits` 调大 |
| 正常回复里 @ 人丢了 | 只会删名单内 QQ 的 @；确认名单没写错 |
| 完全没生效 | 面板确认插件已启用；日志搜 `[botguard]` |
| 想知道拦了哪些 | 日志里搜 `[botguard]`，会打印原因和轮次 |

## 数据与隐私

- 全部逻辑在本地完成，**不联网、不上报任何消息内容**。
- 只持久化两个东西：自动学习到的 QQ 名单、命中次数统计。
- 位置：`<AstrBot数据目录>/plugin_data/astrbot_plugin_botguard/state.json`，删掉即重置。

## 兼容性

- AstrBot ≥ 4.0.0
- 平台：aiocqhttp（QQ 个人号 / OneBot v11）；其他平台逻辑同样可用，但识别依据以 OneBot 原始事件为主

## 许可证

[MIT](./LICENSE)
