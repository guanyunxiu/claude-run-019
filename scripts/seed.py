#!/usr/bin/env python3
"""初始化演示数据：两个相互隔离的租户 + 不同权限身份 + 多份科研文档。

账号（密码统一为 demo123）：
  生物实验室租户 biolab:
    alice@lab.cn   租户管理员（分子生物学团队 / 研发部）
    bob@lab.cn     成员（分子生物学团队 / 研发部）
    carol@lab.cn   成员（细胞生物学团队 / 研发部）
    dave@lab.cn    成员（基因治疗团队 / 临床部）
  化学材料租户 chemmat:
    erin@chem.cn   租户管理员（催化团队 / 材料部）

文档（用于演示三级权限与片段级授权）：
  《新型纳米颗粒递送系统研究报告》 team/分子生物学团队
  《CRISPR 实验记录-2026Q1》      private（Alice 私有，含片段授权给 Bob）
  《研发部年度科研综述》          department/研发部
  《催化剂筛选实验日志》          chemmat 租户团队公开（用于验证租户硬隔离）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import db_global, db_tenant  # noqa: E402
from app.api.documents_service import ingest_upload  # noqa: E402

PASSWORD = "demo123"

NANO_REPORT = """新型纳米颗粒递送系统研究报告

1 摘要
本报告系统评估了脂质-聚合物杂化纳米颗粒（LPN）在小分子药物与核酸药物递送中的表现。
实验结果显示，优化配方 LPN-207 的平均水合粒径为 82 纳米，多分散指数 PDI 为 0.11，
在 4 摄氏度条件下静置 30 天粒径变化小于 5%，具备良好的胶体稳定性。

2 材料与方法
2.1 材料
聚乳酸-羟基乙酸共聚物 PLGA（75:25，分子量 24 kDa）、二硬脂酰磷脂酰胆碱 DSPC、
胆固醇与 DSPE-PEG2000 均购自正规试剂供应商，使用前未做进一步处理。
2.2 纳米颗粒制备
采用纳米沉淀法结合自组装工艺：将 PLGA 溶于乙腈作为有机相，在磁力搅拌条件下
缓慢滴加至含磷脂的水相中，旋蒸除去有机溶剂后经 100 纳米孔径滤膜挤出。
2.3 表征方法
使用动态光散射仪（DLS）测定粒径与 zeta 电位；采用透射电子显微镜（TEM）
观察颗粒形貌；采用超滤离心法测定药物包封率与载药量。

3 实验结果
3.1 粒径与形貌
TEM 图像显示 LPN-207 呈规则球形，核壳结构清晰。DLS 测得平均粒径 82 纳米，
与电镜统计结果一致。
3.2 包封率与释放行为
LPN-207 对模式药物的包封率达到 89.4%，载药量为 6.8%。体外释放实验中，
前 6 小时累计释放约 23%，120 小时累计释放达到 78%，呈现明显的缓释特征。

4 讨论
PEG 外壳显著降低了蛋白吸附，这可能是 LPN-207 在血清中保持稳定的主要原因。
后续将在肝细胞模型中评估其主动靶向能力。

5 结论
优化配方 LPN-207 兼具高包封率、缓释能力与良好稳定性，适合作为下一代
肝靶向核酸药物的候选递送载体。
"""

CRISPR_LOG = """CRISPR 基因编辑实验记录 2026Q1（私有）

1 实验目的
验证针对位点 HBB-sg04 的新型单碱基编辑器 BE4max 的编辑效率与脱靶情况。

2 实验方法
采用电穿孔方式将核糖核蛋白复合物 RNP 递送至人源 HEK293T 细胞，
48 小时后收集细胞提取基因组 DNA，扩增子测序分析编辑效率。

3 关键结果
3.1 编辑效率
三个生物学重复的目标位点 C-to-T 编辑效率分别为 41.2%、43.8% 与 42.5%，
平均编辑效率 42.5%。
3.2 脱靶检测
通过 CIRCLE-seq 检出 12 个潜在脱靶位点，深度测序验证后仅 OT-3 位点
存在 0.4% 的低水平编辑，处于可接受安全范围。
3.3 保密数据（仅限课题负责人与指定成员）
本批次细胞供者编号、原始测序 FASTQ 路径与专利申报策略属于核心保密信息：
供者批次 DONOR-2026-017，原始数据归档于内部存储 /secure/seq/hbb_sg04/，
专利交底书草案编号 P-2026-088，禁止向课题组成员之外传播。

4 下一步计划
下季度将在原代造血干细胞中复测编辑效率，并评估不同电穿孔缓冲液的影响。
"""

DEPT_REVIEW = """研发部 2026 年度科研综述

一、总体进展
本年度研发部共在研课题 14 项，完成中期验收 9 项，申请发明专利 11 项。
科研经费执行率达到 92%，成果转化合同金额较上年度增长 37%。

二、重点方向
在核酸药物递送方向，杂化纳米颗粒平台完成了 3 轮配方迭代；
在基因编辑方向，单碱基编辑器在 HBB 位点的平均编辑效率稳定在 40% 以上；
在类器官方向，肝类器官培养周期由 21 天缩短至 14 天。

三、下年度规划
部门计划建设统一的载体中试平台，并推动两个候选项目进入新药临床试验申报阶段。
"""

CHEM_LOG = """催化剂筛选实验日志（化学材料租户）

1 实验目的
筛选用于二氧化碳加氢制甲醇的高选择性铜锌锆催化剂。

2 结果
编号 CZZ-12 的催化剂在 240 摄氏度、3 兆帕条件下，甲醇选择性达到 87.3%，
二氧化碳单程转化率为 13.1%。该结果为本租户保密数据，其他团队无权访问。
"""


def _ensure_user(gconn, email, name, password=PASSWORD):
    user = db_global.get_user_by_email(gconn, email)
    if user is None:
        uid = db_global.create_user(gconn, email, name, password)
    else:
        uid = user["id"]
    return uid


def _ensure_tenant(gconn, slug, name, admin_email=None):
    tenant = db_global.get_tenant_by_slug(gconn, slug)
    if tenant is None:
        tid = db_global.create_tenant(gconn, slug, name)
        db_tenant.connect(slug, tid).close()
    else:
        tid = tenant["id"]
    if admin_email:
        admin = db_global.get_user_by_email(gconn, admin_email)
        db_global.add_tenant_member(gconn, tid, admin["id"], "admin")
    return tid


def seed_demo():
    db_global.init_db()
    gconn = db_global.connect()

    # 用户
    alice = _ensure_user(gconn, "alice@lab.cn", "爱丽丝")
    bob = _ensure_user(gconn, "bob@lab.cn", "鲍勃")
    carol = _ensure_user(gconn, "carol@lab.cn", "卡罗尔")
    dave = _ensure_user(gconn, "dave@lab.cn", "大卫")
    erin = _ensure_user(gconn, "erin@chem.cn", "艾琳")

    # 租户与成员关系
    biolab = _ensure_tenant(gconn, "biolab", "生物实验室", "alice@lab.cn")
    chemmat = _ensure_tenant(gconn, "chemmat", "化学材料中心", "erin@chem.cn")
    db_global.add_tenant_member(gconn, biolab, bob, "member", "分子生物学团队", "研发部")
    db_global.add_tenant_member(gconn, biolab, carol, "member", "细胞生物学团队", "研发部")
    db_global.add_tenant_member(gconn, biolab, dave, "member", "基因治疗团队", "临床部")
    db_global.add_tenant_member(gconn, chemmat, erin, "admin", "催化团队", "材料部")
    gconn.commit()

    # ---- biolab 文档 ----
    tconn = db_tenant.connect("biolab", biolab)
    doc1 = ingest_upload(
        tconn=tconn, tenant_id=biolab, tenant_slug="biolab",
        owner_user_id=alice, filename="纳米颗粒递送研究报告.txt",
        content=NANO_REPORT.encode("utf-8"), title="新型纳米颗粒递送系统研究报告",
        visibility="team", owner_team="分子生物学团队", owner_dept="研发部",
    )
    print("文档1:", doc1["title"], "片段数:", doc1["chunk_count"])

    doc2 = ingest_upload(
        tconn=tconn, tenant_id=biolab, tenant_slug="biolab",
        owner_user_id=alice, filename="CRISPR实验记录-2026Q1.txt",
        content=CRISPR_LOG.encode("utf-8"), title="CRISPR 实验记录-2026Q1",
        visibility="private", owner_team="分子生物学团队", owner_dept="研发部",
    )
    print("文档2:", doc2["title"], "片段数:", doc2["chunk_count"])

    # 片段级授权：把含“保密数据”的片段单独授权给 Bob（指定用户）
    secret_chunks = tconn.execute(
        "SELECT id, heading_path FROM chunks WHERE document_id=? "
        "AND (heading_path LIKE '%3.3%' OR content LIKE '%保密%')",
        (doc2["document_id"],),
    ).fetchall()
    for c in secret_chunks:
        db_tenant.add_grant(tconn, c["id"], "user", str(bob))
        print(f"  片段授权: chunk {c['id']} -> user:bob")
    db_tenant.bump_document_version(tconn, doc2["document_id"])
    tconn.commit()

    doc3 = ingest_upload(
        tconn=tconn, tenant_id=biolab, tenant_slug="biolab",
        owner_user_id=alice, filename="研发部年度科研综述.txt",
        content=DEPT_REVIEW.encode("utf-8"), title="研发部年度科研综述",
        visibility="department", owner_team="分子生物学团队", owner_dept="研发部",
    )
    print("文档3:", doc3["title"], "片段数:", doc3["chunk_count"])
    tconn.close()

    # ---- chemmat 文档（验证跨租户隔离：biolab 任何人都搜不到） ----
    tconn2 = db_tenant.connect("chemmat", chemmat)
    doc4 = ingest_upload(
        tconn=tconn2, tenant_id=chemmat, tenant_slug="chemmat",
        owner_user_id=erin, filename="催化剂筛选实验日志.txt",
        content=CHEM_LOG.encode("utf-8"), title="催化剂筛选实验日志",
        visibility="team", owner_team="催化团队", owner_dept="材料部",
    )
    print("文档4:", doc4["title"], "片段数:", doc4["chunk_count"], "(租户 chemmat)")
    tconn2.close()
    gconn.close()
    print("\n演示数据初始化完成。")


if __name__ == "__main__":
    seed_demo()
