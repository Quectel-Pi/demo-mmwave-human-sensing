# demo-mmwave-human-sensing

[English](README.md) | 简体中文

基于Quectel Pi M2和R60ABD1毫米波雷达，通过UART1内核驱动获取模组检测结果，
由C程序采集、Python标准库提供本地服务，在HTML页面展示呼吸次数、心跳次数、体动及睡眠信息。

## 1. 开发资料与硬件

- Quectel Pi M2，配套Debian 13系统、M2 SDK及Ubuntu编译环境。
- R60ABD1雷达、HDMI显示屏、键盘/鼠标、USB-A双头线、杜邦线等。
- 模组引脚间距为2.0mm，常见杜邦线为2.54mm，请确认接触可靠。

UART采用115200、8N1、无流控，信号为3.3V TTL，不能将5V接到信号脚。
模组上电后等待至少30秒；安装朝向、倾角及测量距离按模组指南执行，不同安装方式会影响结果。

## 2. 获取代码

先获取M2 SDK包和安装本demo库，仓库地址如下：

https://github.com/Quectel-Pi/QPi-SDK/tree/M2-SDK

https://github.com/Quectel-Pi/demo-mmwave-human-sensing

执行完成后，将demo库移至SDK目录下的 `docs/examples/demo-mmwave-human-sensing/` 文件夹。
文件目录如下：

```text
m2/
├── kernel/               # 内核源码 (6.1.118) + 编译产物
├── overlay/              
├── prebuilds/            
├── tools/                
├── toolchains/          
├── skills/              
├── docs/                 # 文档 (examples/参考)
    ├── examples/demo-mmwave-human-sensing/
        ├── README.md                 # 英文（默认）
        ├── README.zh-CN.md           # 中文
        ├── Makefile                  # 读取 src/，产物输出到 build/
        ├── .gitignore                # 排除 build/、*.pyc、*.log、*.jsonl
        ├── src/                      # 核心源码
            ├── kernel.patch              # 内核驱动、UAPI、SDK 构建集成补丁
            ├── quectel-r60abd1.config
            ├── main.c                    # 采集程序入口
            ├── r60abd1.c / r60abd1.h     # 设备访问接口
            ├── ui_server.py              # 本地网页服务
            └── r60abd1-ui.html           # 网页界面
        └── build/                    # 编译产物，自动生成，不提交
            ├── demo-mmwave-human-sensing
            ├── ui_server.py              # 从 src/ 复制，便于 adb 推送
            ├── r60abd1-ui.html
            ├── libr60abd1.a
            ├── r60abd1_client.o
            └── uapi/linux/r60abd1.h
├── build.sh              # source 后注册 build 命令，并导出交叉编译变量
└── Makefile                      
```


## 3. 首次编译与烧录

### 3.1 编译代码

```bash
cd ~/m2/

# 1.检查安装依赖
./build.sh buildenv
./build.sh check

# 2.应用patch
git apply --check docs/examples/demo-mmwave-human-sensing/src/kernel.patch &&
git apply docs/examples/demo-mmwave-human-sensing/src/kernel.patch

# 3. 编译内核、程序，预置网页并生成完整固件
./build.sh all

# 4.核心编译产物
~/m2/docs/examples/demo-mmwave-human-sensing/build/
├── demo-mmwave-human-sensing
├── ui_server.py
└── r60abd1-ui.html
```

### 3.2 烧录

```
先使用双头USB-A连接编译机和板子ADB口。我们使用瑞芯微开发工具进行烧录。
1. 点击升级固件
2. 点击固件进行文件选择
3. 选择本机update.img固件位置
4. 依次按住板子boot按钮和电源按钮3秒以上，出现LOADER即进入烧录模式
5. 点击升级进行烧录
6. 如果只有ADB设备，可以先点击切换，再重试第四步
```

### 3.3 验证环境

输出对应驱动证明加载成功

#### 3.3.1 adb下

```
adb shell "dmesg | grep -i r60abd1"
adb shell "ls -l /dev/r60abd1"
adb shell "ls -l /opt/demo-mmwave-human-sensing"
```

#### 3.3.2 板子

```
sudo chgrp "$(id -gn)" /dev/r60abd1
sudo chmod g+rw /dev/r60abd1
```


## 4. 运行程序

默认实时模式，尝试打开 http://127.0.0.1:8765/

```
cd ~
/opt/demo-mmwave-human-sensing/demo-mmwave-human-sensing
```

## 5. 单独编译

```
# 单独编译指后续需要基于demo进行改动，又不想重新烧录的情况下使用的方案：一定要先整编烧录新驱动
cd /home/cherin/data/QpiDemo/SDK_M2/m2
make -C docs/examples/demo-mmwave-human-sensing userspace

# 假设地址 cd Y:\QpiDemo\SDK_M2\m2
# 推送上板子
adb push .\docs\examples\demo-mmwave-human-sensing\build\demo-mmwave-human-sensing /opt/demo-mmwave-human-sensing/
adb push .\docs\examples\demo-mmwave-human-sensing\build\ui_server.py /opt/demo-mmwave-human-sensing/
adb push .\docs\examples\demo-mmwave-human-sensing\build\r60abd1-ui.html /opt/demo-mmwave-human-sensing/
adb shell chmod 755 /opt/demo-mmwave-human-sensing/demo-mmwave-human-sensing
然后重新启动 demo，页面强制刷新。仅改 HTML/Python 时无需编译：改 src/ 下的文件后执行 make 把它们复制到 build/，或直接推送 src/ 下对应文件。
```

## 6. 界面与本地存储

分为两个模式，实时模式和睡眠模式，可以互相切换。页面为浅色主题，主要分为以下部分：
1. 实时体征：人体检测、呼吸、心率、体动幅度，以及运动状态、人体距离、探测范围。无人或数据长时间未更新时显示 `--`，不会沿用旧值。
2. 模式信息：实时模式显示呼吸/心跳波形；睡眠模式显示睡眠分期、时长、评分及睡眠报告。
3. 本地历史：可选 1/6/12/24 小时或自定义时间，显示人体存在时间条及呼吸、心率、体动曲线（浅色带为该时间段的最小–最大值），支持导出 CSV/JSONL。
4. 设备状态：默认折叠，仅用于确认驱动是否收到数据（收帧数、协议错误、丢失、波形包数）。

页面支持中文/English，点击右上角的 `EN` / `中文` 按钮切换；首次打开按浏览器语言选择，之后记住上次的选择。

数据说明：
- 人体存在、运动状态、呼吸状态由模组“状态变化时才上报”，且有人变无人最长需约 1 分钟；程序每 3 秒主动查询一次，避免丢包后状态长期不更新。
- 呼吸/心率为 0 表示模组暂未测出，页面显示“测量中”。
- 波形每秒查询一次。若“设备状态”中波形包数一直为 0，说明模组没有返回波形（需人员静坐在 0.4–1.5m 范围内）。
- 本地记录保存在 `~/r60abd1-data/`（JSONL）。以变化事件为主，每分钟附带一次完整快照；异常中断留下的残缺行会被自动跳过。
- 主动查询运动/范围/波形需要新版内核驱动，请整编烧录；旧驱动仍可运行，仅不查询这几项。

## 7. 核心实现

以下为源码节选，实际函数还包含锁、长度/版本校验及错误处理。

### 内核驱动

```c
static const struct serdev_device_ops serial_ops = {
    .receive_buf = receive_buf,
    .write_wakeup = write_wakeup,
};

# 串口收到数据后进入接收回调，缓存并解析完整帧，转换成事件后放入队列。应用侧通过以下入口读取或控制：
.read = radar_read,
.poll = radar_poll,
.unlocked_ioctl = radar_ioctl,
```

作用：封装 UART 通信，向应用提供统一设备接口。

### 数据解析

```c
MSG(0x81, 2, RESP_RATE, BYTE, 1),
MSG(0x85, 2, HEART_RATE, BYTE, 1),
MSG(0x81, 5, RESP_WAVE, WAVE, 5),

# 波形转换核心
case F_WAVE:
    event->count = 5;
    for (i = 0; i < 5; i++)
        event->value[i] = (int)data[i] - 128;
    break;
```

作用：在帧头、长度、校验和及帧尾检查通过后，将协议字节转换为统一事件；波形每包五点，不由心跳、呼吸次数模拟。

### 应用接口

事件读取接口内部使用 poll＋read：

```c
wait = (struct pollfd){.fd = radar->fd, .events = POLLIN};
ret = poll(&wait, 1, timeout_ms);
if (ret <= 0)
    return ret;
length = read(radar->fd, event, sizeof(*event));

# 完整实现还检查读取长度、接口版本和数据范围。上层调用：
ret = r60abd1_get_data(&radar, &event, 200);
```

作用：隐藏设备访问细节，返回 1 表示取得数据、0 表示本次无数据、-1 表示错误；同一接口层还封装初始化、退出和模式控制。

### 显示函数

网页获取后端快照，确认请求仍有效后更新界面：

```javascript
const r = await fetch(
  "/api/snapshot?after="+cursor+"&session="+(session||""),
  {cache:"no-store"});
if (!r.ok) throw Error("HTTP "+r.status);
const s = await r.json();
if (version === requestVersion && !changing) render(s);

# 显示时按模式控制区域：

cards("common", s.common, commonFields);
$("sleep-section").hidden = s.mode !== 1 || s.switching;
$("waveforms").hidden = s.mode !== 0 || s.switching;
```

作用：约每两秒更新通用信息；实时模式显示实际波形，睡眠模式显示睡眠结果，避免不同模式混显。

### 本地存储与查询

```python
volume = int((time.monotonic() - self.started) // RECORD_VOLUME_SECONDS)
if self.volume != volume:
    if self.record is not None:
        self.record.close()
    path = self.directory / "{}-{}-{:04d}.jsonl".format(
        self.prefix, self.started_ns, volume)
    self.record = path.open("x", encoding="utf-8")
    self.volume = volume
```

使用独占创建避免覆盖文件，定期写入并刷新缓冲，退出补写最终快照。损坏记录和写入失败会明确报错。

## 8. 常见问题与贡献

DP1-有无人：
模组的DP1有无人检测算法实际测量误差比较大，切换不灵敏。淘宝厂家的回复是仅作参考参数，且跟安装方式关系较大。

DP5-人体方位主动上报：
该功能在用户手册的数据定义模糊，既有"首位为0表示为负，首位为1 表示为正"，也有"首位为1表示为负，首位为0 表示为正"，通过询问淘宝厂家与模组原厂，确认首位1为负。

如在使用过程中遇到问题，欢迎在[移远官方论坛](https://forumschinese.quectel.com/c/quectel-pi/58) 提交技术咨询，我们的技术支持团队将及时为您解答。
欢迎提交Issue和Pull Request来改进此项目。

Copyright © Quectel Wireless Solutions Co., Ltd. 2026. All rights reserved.
