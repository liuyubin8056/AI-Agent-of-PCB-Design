"""参考子电路库：把「经典电路」变成可复用的确定性生成器。

这是控制幻觉的第二道防线。做法不是让 LLM 从零编网表，而是：

    子电路模板（Python 函数，绝对可靠）
        +  LLM 只负责「选哪几个模板、填什么参数」
        =  引脚级网表

模板内部也不硬编码引脚号，而是按引脚语义在器件库里查找
（找名字含 RESET 的脚、找 type=power_in 的脚、找 XTAL1/XTAL2 ……），
所以换一颗 MCU，模板照样能用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .ir import BomItem, Net
from .library import Library


# --------------------------------------------------------------------------- #
# 位号分配器：保证多个子电路拼装时位号不冲突
# --------------------------------------------------------------------------- #
class RefPool:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._used: set[str] = set()

    def take(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0)
        while True:
            n += 1
            ref = f"{prefix}{n}"
            if ref not in self._used:
                self._counters[prefix] = n
                self._used.add(ref)
                return ref

    def reserve(self, ref: str) -> str:
        self._used.add(ref)
        return ref


@dataclass
class Fragment:
    """一个子电路的产物：若干器件 + 若干网络。"""

    id: str
    parts: list[BomItem] = field(default_factory=list)
    nets: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 引脚语义查找
# --------------------------------------------------------------------------- #
def _pin_map(lib: Library, sid: str) -> dict[str, str]:
    """把引脚名拆成「首段 → 引脚号」，例如 ~RESET/PC6 -> {'~RESET': '1'}。"""
    mapping: dict[str, str] = {}
    for p in lib.pins(sid):
        head = p["name"].split("/")[0].strip()
        mapping.setdefault(head, p["number"])
        mapping.setdefault(p["name"], p["number"])
    return mapping


def _find_pin(lib: Library, sid: str, needle: str) -> str | None:
    for p in lib.pins(sid):
        if needle.upper() in p["name"].upper():
            return p["number"]
    return None


def _find_pins_of_type(lib: Library, sid: str, pin_type: str) -> list[str]:
    return [p["number"] for p in lib.pins(sid) if p["type"] == pin_type]


# --------------------------------------------------------------------------- #
# 模板 1：MCU 最小系统
# --------------------------------------------------------------------------- #
def build_mcu_min_system(
    pool: RefPool, ctx: dict[str, Any], lib: Library, **params: Any
) -> Fragment:
    mcu_sid = lib.resolve(params.get("mcu", "pcbagent:MCU_ATmega328P"))
    crystal_value = str(params.get("crystal", "16MHz"))
    load_cap = str(params.get("load_cap", "22pF"))
    decoupling = str(params.get("decoupling", "100nF"))
    reset_pullup = str(params.get("reset_pullup", "10k"))
    rail = ctx["rail"]
    gnd = ctx["gnd"]

    u = pool.take(lib.ref_prefix(mcu_sid))
    frag = Fragment(id="mcu_min_system")
    frag.parts.append(
        BomItem(u, mcu_sid, lib.default_value(mcu_sid), lib.footprint(mcu_sid),
                role="主控 MCU", note="最小系统核心")
    )

    pins = _pin_map(lib, mcu_sid)
    ctx["mcu_ref"] = u
    ctx["mcu_pins"] = pins
    ctx["mcu_symbol"] = mcu_sid

    rail_members: list[str] = []
    gnd_members: list[str] = []

    # 1) 电源与地：按引脚类型 + 名字判定
    for p in lib.pins(mcu_sid):
        if p["type"] != "power_in":
            continue
        if "GND" in p["name"].upper():
            gnd_members.append(f"{u}.{p['number']}")
        else:
            rail_members.append(f"{u}.{p['number']}")
            # 每个电源脚配一颗去耦电容
            c = pool.take("C")
            frag.parts.append(
                BomItem(c, "pcbagent:CAP", decoupling, lib.footprint("pcbagent:CAP"),
                        role=f"{p['name']} 去耦", note=f"紧靠 {u} 引脚 {p['number']}")
            )
            rail_members.append(f"{c}.1")
            gnd_members.append(f"{c}.2")

    # 2) 复位：上拉电阻 + 按键
    reset_pin = _find_pin(lib, mcu_sid, "RESET")
    if reset_pin:
        r = pool.take("R")
        frag.parts.append(
            BomItem(r, "pcbagent:RES", reset_pullup, lib.footprint("pcbagent:RES"),
                    role="复位上拉", note="防止复位脚悬空误触发")
        )
        sw = pool.take("SW")
        frag.parts.append(
            BomItem(sw, "pcbagent:SW_PUSH", "SW_PUSH", lib.footprint("pcbagent:SW_PUSH"),
                    role="复位按键", note="按下拉低复位")
        )
        frag.nets["RESET"] = [f"{u}.{reset_pin}", f"{r}.1", f"{sw}.1"]
        rail_members.append(f"{r}.2")
        gnd_members.append(f"{sw}.2")

    # 3) 晶振：两颗负载电容对地
    xtal1 = _find_pin(lib, mcu_sid, "XTAL1")
    xtal2 = _find_pin(lib, mcu_sid, "XTAL2")
    if xtal1 and xtal2:
        y = pool.take("Y")
        frag.parts.append(
            BomItem(y, "pcbagent:XTAL", crystal_value, lib.footprint("pcbagent:XTAL"),
                    role="时钟晶振", note="时钟源")
        )
        c1 = pool.take("C")
        c2 = pool.take("C")
        for c in (c1, c2):
            frag.parts.append(
                BomItem(c, "pcbagent:CAP", load_cap, lib.footprint("pcbagent:CAP"),
                        role="晶振负载电容", note="与晶振匹配，典型 22pF")
            )
        frag.nets["XTAL1"] = [f"{u}.{xtal1}", f"{y}.1", f"{c1}.1"]
        frag.nets["XTAL2"] = [f"{u}.{xtal2}", f"{y}.2", f"{c2}.1"]
        gnd_members.extend([f"{c1}.2", f"{c2}.2"])

    # 4) AREF 滤波电容
    aref = _find_pin(lib, mcu_sid, "AREF")
    if aref:
        c = pool.take("C")
        frag.parts.append(
            BomItem(c, "pcbagent:CAP", decoupling, lib.footprint("pcbagent:CAP"),
                    role="AREF 滤波", note="稳定 ADC 参考")
        )
        frag.nets["AREF"] = [f"{u}.{aref}", f"{c}.1"]
        gnd_members.append(f"{c}.2")

    frag.nets[rail] = rail_members
    frag.nets[gnd] = gnd_members
    frag.notes.append(f"主控 {u} 使用 {crystal_value} 晶振")
    return frag


# --------------------------------------------------------------------------- #
# 模板 2：5V 稳压输入
# --------------------------------------------------------------------------- #
def build_power_ldo5v(
    pool: RefPool, ctx: dict[str, Any], lib: Library, **params: Any
) -> Fragment:
    ldo_sid = "pcbagent:LDO_AMS1117_5V"
    cin = str(params.get("cin", "10uF"))
    cout = str(params.get("cout", "10uF"))
    rail = ctx["rail"]
    gnd = ctx["gnd"]

    j = pool.take("J")
    u = pool.take("U")
    c_in = pool.take("C")
    c_out = pool.take("C")

    frag = Fragment(id="power_ldo5v")
    frag.parts.extend(
        [
            BomItem(j, "pcbagent:CONN_2", "Conn_01x02", lib.footprint("pcbagent:CONN_2"),
                    role="电源输入", note="外接 7~12V 适配器"),
            BomItem(u, ldo_sid, lib.default_value(ldo_sid), lib.footprint(ldo_sid),
                    role="线性稳压", note="7~12V 降至 5V"),
            BomItem(c_in, "pcbagent:CAP", cin, lib.footprint("pcbagent:CAP"),
                    role="输入滤波", note="抑制上电冲击"),
            BomItem(c_out, "pcbagent:CAP", cout, lib.footprint("pcbagent:CAP"),
                    role="输出滤波", note="稳定 5V 输出"),
        ]
    )
    frag.nets["VIN_RAW"] = [f"{j}.1", f"{u}.3", f"{c_in}.1"]
    frag.nets[rail] = [f"{u}.2", f"{c_out}.1"]
    frag.nets[gnd] = [f"{j}.2", f"{u}.1", f"{c_in}.2", f"{c_out}.2"]
    return frag


# --------------------------------------------------------------------------- #
# 模板 3：LED 指示
# --------------------------------------------------------------------------- #
def build_led_indicator(
    pool: RefPool, ctx: dict[str, Any], lib: Library, **params: Any
) -> Fragment:
    mcu_ref = ctx.get("mcu_ref")
    mcu_pins = ctx.get("mcu_pins") or {}
    drive_pin = str(params.get("drive_pin", "PD7"))
    pin_no = mcu_pins.get(drive_pin) or _find_pin(
        lib, ctx.get("mcu_symbol", "pcbagent:MCU_ATmega328P"), drive_pin
    )
    if not mcu_ref or not pin_no:
        return Fragment(id="led_indicator", notes=["未找到主控，跳过 LED 指示"])

    resistor = str(params.get("resistor", "330"))
    color = str(params.get("color", "LED-Green"))

    r = pool.take("R")
    d = pool.take("D")
    frag = Fragment(id="led_indicator")
    frag.parts.extend(
        [
            BomItem(r, "pcbagent:RES", resistor, lib.footprint("pcbagent:RES"),
                    role="LED 限流", note="限制 LED 电流约 10mA"),
            BomItem(d, "pcbagent:LED", color, lib.footprint("pcbagent:LED"),
                    role="状态指示灯", note="心跳 / 状态指示"),
        ]
    )
    frag.nets["LED_CTRL"] = [f"{mcu_ref}.{pin_no}", f"{r}.1"]
    frag.nets["LED_A"] = [f"{r}.2", f"{d}.2"]
    frag.nets.setdefault(ctx["gnd"], []).append(f"{d}.1")
    return frag


# --------------------------------------------------------------------------- #
# 模板 4：I2C 传感器接口
# --------------------------------------------------------------------------- #
def build_i2c_sensor(
    pool: RefPool, ctx: dict[str, Any], lib: Library, **params: Any
) -> Fragment:
    mcu_ref = ctx.get("mcu_ref")
    mcu_symbol = ctx.get("mcu_symbol", "pcbagent:MCU_ATmega328P")
    sda = _find_pin(lib, mcu_symbol, "SDA")
    scl = _find_pin(lib, mcu_symbol, "SCL")
    if not mcu_ref or not sda or not scl:
        return Fragment(id="i2c_sensor", notes=["未找到 I2C 引脚，跳过传感器接口"])

    pullup = str(params.get("pullup", "4.7k"))
    rail = ctx["rail"]
    gnd = ctx["gnd"]

    u = pool.take("U")
    r1 = pool.take("R")
    r2 = pool.take("R")
    frag = Fragment(id="i2c_sensor")
    frag.parts.extend(
        [
            BomItem(u, "pcbagent:SENSOR_I2C", "I2C_SENSOR", lib.footprint("pcbagent:SENSOR_I2C"),
                    role="I2C 传感器接口", note="4 线模块接口"),
            BomItem(r1, "pcbagent:RES", pullup, lib.footprint("pcbagent:RES"),
                    role="SDA 上拉", note="I2C 开漏必需"),
            BomItem(r2, "pcbagent:RES", pullup, lib.footprint("pcbagent:RES"),
                    role="SCL 上拉", note="I2C 开漏必需"),
        ]
    )
    frag.nets["SDA"] = [f"{mcu_ref}.{sda}", f"{u}.3", f"{r1}.1"]
    frag.nets["SCL"] = [f"{mcu_ref}.{scl}", f"{u}.4", f"{r2}.1"]
    frag.nets[rail] = [f"{u}.1", f"{r1}.2", f"{r2}.2"]
    frag.nets[gnd] = [f"{u}.2"]
    return frag


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
@dataclass
class SubCircuit:
    id: str
    name: str
    desc: str
    params: dict[str, str]
    build: Callable[..., Fragment]

    def prompt_line(self) -> str:
        ps = ", ".join(f"{k}={v}" for k, v in self.params.items()) or "无"
        return f"- {self.id} | {self.name} | {self.desc} | 参数: {ps}"


SUB_CIRCUITS: dict[str, SubCircuit] = {
    "mcu_min_system": SubCircuit(
        "mcu_min_system",
        "MCU 最小系统",
        "主控 + 晶振 + 复位 + 逐脚去耦 + AREF 滤波",
        {"mcu": "器件库 lib_id", "crystal": "晶振频率", "load_cap": "负载电容",
         "decoupling": "去耦电容", "reset_pullup": "复位上拉阻值"},
        build_mcu_min_system,
    ),
    "power_ldo5v": SubCircuit(
        "power_ldo5v",
        "5V 稳压输入",
        "电源接口 + AMS1117-5.0 + 输入输出滤波",
        {"cin": "输入电容", "cout": "输出电容"},
        build_power_ldo5v,
    ),
    "led_indicator": SubCircuit(
        "led_indicator",
        "LED 状态指示",
        "GPIO 经限流电阻驱动 LED 到地",
        {"drive_pin": "驱动引脚名，如 PD7", "resistor": "限流电阻", "color": "LED 颜色"},
        build_led_indicator,
    ),
    "i2c_sensor": SubCircuit(
        "i2c_sensor",
        "I2C 传感器接口",
        "4 线模块接口 + SDA/SCL 上拉",
        {"pullup": "上拉阻值"},
        build_i2c_sensor,
    ),
}

_BUILD_ORDER = {"mcu_min_system": 0, "power_ldo5v": 1}


def subcircuits_prompt_text() -> str:
    return "\n".join(sc.prompt_line() for sc in SUB_CIRCUITS.values())


def build_board(
    selections: list[dict[str, Any]],
    lib: Library | None = None,
    rail: str = "+5V",
    gnd: str = "GND",
) -> tuple[list[BomItem], list[Net], list[str]]:
    """按选择的子电路拼出一整块板。

    ``selections`` 形如 ``[{"id": "mcu_min_system", "params": {...}}, ...]``，
    这正是 LLM 需要产出的东西——它只做「选择题 + 填空题」。
    """
    lib = lib or Library()
    pool = RefPool()
    ctx: dict[str, Any] = {"lib": lib, "rail": rail, "gnd": gnd}

    ordered = sorted(
        enumerate(selections),
        key=lambda pair: (_BUILD_ORDER.get(str(pair[1].get("id")), 5), pair[0]),
    )

    parts: list[BomItem] = []
    nets: dict[str, list[str]] = {}
    log: list[str] = []

    for _, sel in ordered:
        sid = str(sel.get("id", "")).strip()
        if sid not in SUB_CIRCUITS:
            log.append(f"跳过未知子电路 {sid!r}")
            continue
        params = dict(sel.get("params") or {})
        frag = SUB_CIRCUITS[sid].build(pool, ctx, lib, **params)
        parts.extend(frag.parts)
        for name, members in frag.nets.items():
            bucket = nets.setdefault(name, [])
            for m in members:
                if m not in bucket:
                    bucket.append(m)
        log.extend(frag.notes)

    net_list = [
        Net(name=name, members=[m for m in members if m])
        for name, members in nets.items()
        if members
    ]
    net_list.sort(key=lambda n: ({"power": 0, "ground": 1}.get(n.kind, 2), n.name))
    return parts, net_list, log
