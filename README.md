# 025209 存储基金监控面板

监控 **永赢先锋半导体智选混合发起C（025209）**：

- 最新单位净值及相对阶段高点回撤
- 2.25–2.35 元观察区、2.25 元风险线、2.50 元修复线
- 前十大重仓股与当日行情广度
- DRAM / NAND Flash 合约价格方向
- 触发关键信号时自动创建 GitHub Issue
- GitHub Pages 跨终端访问，支持安装为 PWA

## 部署

1. 创建一个公开 GitHub 仓库，例如 `fund-monitor-025209`。
2. 把本项目全部文件上传到仓库默认分支 `main`。
3. 进入 **Settings → Pages → Build and deployment → Source**，选择 **GitHub Actions**。
4. 打开 **Actions**，手动运行 `Update fund data and deploy Pages`，或等待首次 push 自动执行。
5. 部署完成后，在 **Settings → Pages** 点击网站地址。

项目型 Pages 地址通常为：

```text
https://<你的GitHub用户名>.github.io/fund-monitor-025209/
```

## 自动更新

工作流默认在周一至周五北京时间 21:30 运行。GitHub Actions 的定时任务可能有数分钟延迟。

## 数据源与限制

- 基金净值、定期持仓与股票行情：东方财富公开页面/接口。
- 存储行业价格信号：TrendForce 公开新闻页面。
- 基金持仓仅按定期报告披露，不代表实时仓位。
- 数据源网页结构变化时，抓取脚本可能需要维护。
- 本项目仅供研究，不构成投资建议。
