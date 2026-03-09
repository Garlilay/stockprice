# 资金关系网络分析系统

一个用于追踪资金分析中人员关系、链上地址的可视化工具。

## 功能

- **人员管理** — 记录人员基本信息、角色、机构、风险等级、标签
- **链上地址** — 为每个人员关联多个链上地址（ETH / BTC / BSC / SOL / TRON 等）
- **关系网络** — 建立人员之间的关系（同事、合作、朋友、家人、投资、可疑）
- **可视化画布** — 基于 Cytoscape.js 的交互式关系图，支持拖拽、缩放、导出
- **搜索** — 按姓名、机构、链上地址搜索

## 快速启动

```bash
cd fund_analysis
pip install -r requirements.txt
python app.py
```

浏览器访问 http://localhost:5001

## 数据库结构

| 表 | 说明 |
|---|---|
| `persons` | 人员基本信息 |
| `addresses` | 链上地址（多链支持） |
| `relationships` | 人员关系（类型 + 强度） |
| `transactions` | 链上交易记录 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/persons` | 获取所有人员 |
| POST | `/api/persons` | 添加人员 |
| PUT | `/api/persons/:id` | 更新人员 |
| DELETE | `/api/persons/:id` | 删除人员 |
| POST | `/api/addresses` | 添加地址 |
| DELETE | `/api/addresses/:id` | 删除地址 |
| GET | `/api/relationships` | 获取所有关系 |
| POST | `/api/relationships` | 建立关系 |
| DELETE | `/api/relationships/:id` | 删除关系 |
| GET | `/api/graph` | 获取图数据（Cytoscape 格式） |
| GET | `/api/search?q=` | 搜索 |
