"""生成合成浏览器样本；真实模板通过 P06_SAMPLE 传路径，不复制到 Git。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.p06.samples import sample

if __name__ == "__main__":
    root=Path("data/private/p06");root.mkdir(parents=True,exist_ok=True)
    for name,kwargs in [("synthetic",{}),("synthetic-reordered",{"order":(3,1,2)}),("missing-sheet",{"missing_sheet":True}),("missing-key",{"missing_key":True}),("ambiguous",{"ambiguous":True})]:
        (root/f"{name}.xlsx").write_bytes(sample(**kwargs))
    print(f"合成验收样本：{root.resolve()}（3 商品、3 装箱成员、图片三种状态）")
