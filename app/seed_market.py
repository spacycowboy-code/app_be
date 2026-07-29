"""DEPRECATED – Seed script is no longer needed.

The 18 mock items it created have been removed from production.
Market data now comes solely from GitLab sync and manual uploads.
This file is kept only for reference; do NOT run it.
"""
from app.database import SessionLocal
from app.models.market import RpAiMarketItem

# (name, dept, deptColor, categoryId, description, serviceType, itemType,
#  rating, users, isTrending, responseTime, version, author, tags, verified)
_ITEMS = [
    ("ADS 仿真解析器", "Radio Design 1", "#1174e6", "radio-design-1", "解析 Keysight ADS 仿真输出，自动提取 S 参数、P1dB、PAE 等 RF 关键指标并生成分析报告", "Code", "Tool", 4.9, "2.1K", True, "<200ms", "2.3", "RD1 Team", ["ADS", "S参数", "RF仿真"], True),
    ("PA 仿真分析 Agent", "Radio Design 1", "#1174e6", "radio-design-1", "读取 ADS 仿真结果 → 分析增益效率 → 给出调优建议 → 自动更新设计文档的全链路分析 Agent", "云服务", "Agent", 4.8, "1.2K", True, "~8s", "1.5", "RD1 Team", ["PA", "功放", "多步骤"], True),
    ("Gerber 审查工具", "Radio Design 2", "#06b6d4", "radio-design-2", "上传 PCB Gerber 文件，自动检查 DFM 规则、阻抗匹配与走线等长，输出带标注的审查报告", "Code", "Tool", 4.7, "1.5K", False, "<500ms", "3.1", "RD2 Team", ["PCB", "Gerber", "DFM"], True),
    ("PCB 审查 Agent", "Radio Design 2", "#06b6d4", "radio-design-2", "检查 Gerber → 信号完整性分析 → 热仿真评估 → 输出综合审查报告并提出分级改善建议", "云服务", "Agent", 4.7, "760", False, "~12s", "1.2", "RD2 Team", ["信号完整性", "热仿真", "全链路"], False),
    ("HFSS 天线仿真接口", "Radio Design 3", "#10b981", "radio-design-3", "调用 HFSS/CST 仿真接口，提取三维方向图，计算主波束增益、旁瓣电平与端口隔离度", "Code", "Tool", 4.6, "980", False, "~4min", "2.0", "RD3 Team", ["HFSS", "天线", "方向图"], True),
    ("天线阵列优化 Agent", "Radio Design 3", "#10b981", "radio-design-3", "输入阵列约束条件 → 调用仿真 → 多目标优化波束赋形权重 → 输出最优方向图与分析报告", "云服务", "Agent", 4.5, "540", False, "~15s", "1.1", "RD3 Team", ["波束赋形", "优化", "Massive MIMO"], False),
    ("JTAG 调试接口", "Radio SW", "#22c55e", "radio-sw", "通过 OpenOCD 连接目标板，支持固件烧录、寄存器读写与实时断点调试，支持 ARM/RISC-V", "Code", "Tool", 4.6, "1.8K", False, "<100ms", "4.2", "SW Team", ["JTAG", "OpenOCD", "调试"], True),
    ("固件 CI/CD 工具", "Radio SW", "#22c55e", "radio-sw", "触发 Jenkins 构建、执行单元测试、静态分析、合规检查，生成 Release Notes 并发布到固件仓库", "云服务", "Tool", 4.8, "2.6K", True, "~3min", "3.0", "SW Team", ["Jenkins", "CI/CD", "自动化"], True),
    ("固件发布 Agent", "Radio SW", "#22c55e", "radio-sw", "CI 构建 → 单元测试 → 合规检查 → Release Notes 生成 → 自动发布的全流程自主 Agent", "云服务", "Agent", 4.9, "1.5K", True, "~5min", "2.1", "SW Team", ["固件", "发布", "全链路"], True),
    ("VISA 测量仪器采集", "IVC", "#f97316", "ivc", "通过 VISA 接口控制 R&S/Keysight 仪器，自动采集 EVM、ACLR、Pout、NF 等 RF 测量数据", "Code", "Tool", 4.8, "3.2K", True, "<1s", "3.5", "IVC Team", ["VISA", "EVM", "自动测量"], True),
    ("RF 测试全链路 Agent", "IVC", "#f97316", "ivc", "仪器触发 → 数据采集 → EVM/ACLR 分析 → 对比规格基线 → 生成测试报告 → 推送知识库", "云服务", "Agent", 4.9, "2.1K", True, "~20s", "4.0", "IVC Team", ["RF测试", "报告", "全链路"], True),
    ("Jira 测试用例同步", "IVC", "#f97316", "ivc", "连接 Jira/Zephyr，同步 IVC 测试用例执行状态，生成覆盖率矩阵并自动创建不合格项工单", "云服务", "Tool", 4.7, "1.1K", False, "~2s", "2.2", "IVC Team", ["Jira", "Zephyr", "覆盖率"], False),
    ("8D 报告生成工具", "QA", "#ef4444", "qa", "录入现场数据与根因分析结论，自动生成符合 Ericsson 标准模板的 8D 报告，支持多语言", "云服务", "Tool", 4.6, "860", False, "~5s", "1.8", "QA Team", ["8D", "质量", "报告模板"], True),
    ("质量根因分析 Agent", "QA", "#ef4444", "qa", "整合现场反馈与测试数据 → AI 辅助根因定位 → 自动生成 8D 报告 → 追踪改善进度闭环", "云服务", "Agent", 4.7, "880", False, "~18s", "2.0", "QA Team", ["根因", "8D", "闭环追踪"], True),
    ("甘特图生成工具", "PMO", "#a855f7", "pmo", "读取里程碑计划表，生成交互式甘特图，支持关键路径高亮，可导出为 PowerPoint 或 PDF", "Code", "Tool", 4.9, "1.7K", True, "<3s", "2.5", "PMO Team", ["甘特图", "里程碑", "导出"], True),
    ("项目周报 Agent", "PMO", "#a855f7", "pmo", "汇总各部门里程碑状态 → 识别延期风险 → 生成 PMO 周报 → 自动发送邮件至管理层", "云服务", "Agent", 4.8, "1.8K", True, "~10s", "3.0", "PMO Team", ["周报", "风险识别", "自动发送"], True),
    ("需求矩阵生成工具", "Product System", "#6366f1", "product-system", "解析产品规格书，自动提取需求条目，生成结构化需求矩阵 Excel 并标注跨部门冲突", "云服务", "Tool", 4.7, "1.2K", False, "~6s", "2.1", "PS Team", ["需求", "规格书", "Excel"], True),
    ("系统需求追溯 Agent", "Product System", "#6366f1", "product-system", "解析规格书 → 生成需求矩阵 → 追踪变更历史 → 识别接口冲突 → 同步至 Jira 的全链路 Agent", "云服务", "Agent", 4.8, "980", False, "~15s", "1.5", "PS Team", ["需求追溯", "变更管理", "全链路"], True),
]


def main() -> None:
    created = 0
    with SessionLocal() as db:
        for i, row in enumerate(_ITEMS, start=1):
            (name, dept, color, cat_id, desc, svc, itype, rating, users,
             trending, rt, ver, author, tags, verified) = row
            item_id = f"seed-{i}"
            item = db.get(RpAiMarketItem, item_id)
            if item is None:
                item = RpAiMarketItem(id=item_id)
                db.add(item)
                created += 1
            item.name = name
            item.description = desc
            item.item_type = "tool" if itype == "Tool" else "agent"
            item.service_type = "code" if svc == "Code" else "cloud"
            item.source = "manual"
            item.category = dept
            item.category_id = cat_id
            item.dept_color = color
            item.tags = tags
            item.author = author
            item.version = ver
            item.rating = rating
            item.users = users
            item.response_time = rt
            item.is_trending = trending
            item.verified = verified
        db.commit()
    print(f"Market seed complete: {created} new items ({len(_ITEMS)} total).")


if __name__ == "__main__":
    main()
