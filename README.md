# Medical PhD Jobs — Shanghai & Suzhou

## 项目用途

这是一个面向医学博士的个人求职信息收集静态网站，聚焦上海和苏州的医药行业岗位。网站支持关键词搜索、筛选、日期排序、浏览器收藏，以及 `Medical PhD Fit` 匹配等级和职位方向统计。

当前 `data/jobs.json` 中的岗位均为明确标记的 `SAMPLE` 示例数据，不代表真实招聘信息。使用前请替换为自行核实的岗位信息和原始链接。

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
│   ├── jobs.json    岗位数据
│   └── companies.json 公司数据
└── README.md        项目说明
```

## 如何添加岗位

编辑 `data/jobs.json`，在数组中增加一个对象。建议保留以下字段：

- `id`：唯一 ID
- `company`、`title`、`city`、`location`
- `direction`、`degree`、`major`、`experience`
- `medicalPhdFit`：填写 `A`、`B`、`C` 或 `D`，分别表示高度相关、相关、可能适合、低相关
- `salary`、`date`、`source`、`summary`、`url`、`tags`
- `sample`：示例数据设为 `true`；真实岗位可删除该字段或设为 `false`

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
