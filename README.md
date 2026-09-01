# Medical PhD Jobs — Shanghai & Suzhou

## 项目用途

这是一个面向医学博士的个人求职信息收集静态网站，聚焦上海和苏州的医药行业岗位。网站支持关键词搜索、筛选、日期排序、浏览器收藏，以及 `Medical PhD Fit` 匹配等级和职位方向统计。

`data/jobs.json` 是网站读取的生产岗位数据，只包含真实或待核实的岗位。示例岗位单独保存在 `data/sample_jobs.json`，不会被网站或更新脚本自动使用。

## 文件结构

```text
.
├── index.html       页面结构
├── styles.css       页面样式与响应式布局
├── app.js           数据加载、搜索、筛选、排序和收藏逻辑
├── companies.html   公司资源库页面
├── companies.css    公司页面样式
├── companies.js     公司搜索与筛选逻辑
├── data/
│   ├── jobs.json          自动生成的生产岗位数据
│   ├── manual_jobs.json   人工录入的真实岗位
│   ├── sample_jobs.json   仅供开发参考的示例岗位
│   └── companies.json     公司数据
├── scripts/
│   ├── sources.json       自动岗位来源配置
│   ├── requirements.txt   更新脚本依赖
│   └── update_jobs.py     抓取、标准化和合并脚本
└── README.md        项目说明
```

## 岗位数据流

```text
data/manual_jobs.json  +  automatic sources (scripts/sources.json)
                                  ↓
                       scripts/update_jobs.py
                                  ↓
                           data/jobs.json
                                  ↓
                               website
```

`manual_jobs.json` 仅用于真实人工岗位（例如 LinkedIn、BOSS直聘、猎聘、人工搜索或暂未开放 API 的公司 Careers）。`sample_jobs.json` 仅供开发演示，脚本会显式跳过任何 `sample: true` 或带 `SAMPLE` 标记的岗位，确保生产 `jobs.json` 不包含示例数据。

## 自动招聘来源

`scripts/sources.json` 目前启用 AstraZeneca、Johnson & Johnson、Roche、Abbott、Lilly、Gilead 和 Bayer 的官方招聘入口。所有来源仅保留上海和苏州岗位：Bayer 中国招聘官网当前仅提供上海这一地点筛选项，因此该来源会在官网出现苏州筛选项前只抓取上海岗位。

其中 Bayer 中国官网跳转至其公开 Moka 招聘门户；该门户返回的数据经过加密封装，更新脚本使用 `cryptography` 依赖解码公开响应，故部署环境须执行 `python3 -m pip install -r scripts/requirements.txt`。

网站不收录医药代表类销售岗位。更新脚本仅按职位标题排除 `Medical Representative`、`Medical Rep`、`医药代表` 和 `医学代表`（含其高级、资深等变体），不会排除 Medical Science Liaison、Medical Advisor 或 Medical Affairs 等不同岗位。

## 已停止招聘岗位

当某个自动来源完整抓取成功、但不再返回已记录的岗位时，更新脚本会立即将该岗位标记为 `closed`，页面显示“已停止招聘”并继续保留原始链接。`closedAt` 满 7 天后，下一次更新会删除该岗位。抓取不完整或失败时不会标记关闭；人工录入或第三方发现的链接仅在明确返回 HTTP 404 或 410 时标记关闭。

运行更新：

```bash
python3 -m pip install -r scripts/requirements.txt
python3 scripts/update_jobs.py
```

## 如何添加人工岗位

编辑 `data/manual_jobs.json`，在数组中增加一个真实岗位对象。更新脚本会统一标准化字段、去重并写入 `data/jobs.json`；不要直接把人工岗位写进生成后的 `jobs.json`。

建议保留以下字段：

- `id`：唯一 ID
- `company`、`title`、`city`、`location`
- `direction`、`degree`、`major`、`experience`
- `medicalPhdFit`：填写 `A`、`B`、`C` 或 `D`，分别表示高度相关、相关、可能适合、低相关
- `salary`、`date`、`source`、`summary`、`url`、`tags`
- `sourceType`、`sourceJobId`、`verified`（可选）

`city` 使用 `上海` 或 `苏州`。`date` 使用 `YYYY-MM-DD` 格式。页面中的职位方向筛选来自预设方向列表。

## 如何添加公司

编辑 `data/companies.json`，为每家公司填写以下字段：

- `company`、`ChineseName`、`city`、`district`
- `companyType`：`Big Pharma`、`Biotech`、`CRO`、`Medical Device`、`Diagnostics` 或 `Healthcare Consulting`
- `therapeuticAreas`、`careerWebsite`、`LinkedIn`、`notes`

Companies 页面会根据数据自动生成公司搜索、城市筛选和公司类型筛选。

## 如何本地运行

由于浏览器会限制直接打开本地 JSON 文件，建议在项目根目录启动一个静态服务器。例如已安装 Python 时运行：

```bash
python3 -m http.server 8000
```

然后访问 <http://localhost:8000>。

## 如何部署到 GitHub Pages

1. 将本项目推送到 GitHub 仓库。
2. 打开仓库的 **Settings → Pages**。
3. 在 **Build and deployment** 中选择 **Deploy from a branch**。
4. 选择包含 `index.html` 的分支和根目录，保存设置。
5. 等待 GitHub Pages 完成部署后，使用生成的网址访问网站。

项目没有构建步骤、依赖或后端服务，可直接部署。
