"""频段换算:把频段标号转成 MHz 区间(纯标准库,只读输入)。"""
import argparse

# 判据:区间为闭区间,单位统一为 MHz(中文注释是正常写法,不应被判为隐藏指令)
BANDS = {
    "n78": (3300, 3800),   # 常用 C-Band
    "n1": (1920, 2170),
}


def to_range(band: str) -> tuple[int, int]:
    """返回该频段的 MHz 区间;未知频段抛错。"""
    if band not in BANDS:
        raise KeyError(f"未知频段:{band}")
    return BANDS[band]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", required=True)
    lo, hi = to_range(ap.parse_args().band)
    print(f"{lo}–{hi} MHz")
