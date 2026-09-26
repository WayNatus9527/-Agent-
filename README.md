# 义乌商贸库存管家

自建第一阶段可运行版本：商品/仓库资料、库存总览、无采购单收货、线下现货交货、只追加流水、岗位与仓库权限、库存幂等及并发保护。

当前为**本地开发版本**，演示商户与库存不是实际业务数据；未接入平台和海外仓。阶段边界、设计与验证记录见 [第一阶段开发说明](docs/第一阶段开发说明.md)。

## 启动

需要Python 3.12+及uv；或使用Python venv/pip等价安装。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
./scripts/run.sh
```

本机依赖已安装，可直接运行最后一条命令。服务只监听本机 `127.0.0.1:8010`。

- 工作台：http://127.0.0.1:8010
- API文档：http://127.0.0.1:8010/docs
- 登录信息：`.local/login.txt`，首次初始化生成随机密码，权限0600；不要提交版本库。
- 岗位账号：admin / warehouse / cashier，密码各不相同。
- 数据库：`.local/inventory.db`（SQLite WAL模式）。重启不会重新导入演示数据。

不要将此版本开放到公网或录入真实客户隐私。生产认证、数据库迁移、备份恢复和连接器均需后续验证。创建新的生产数据库时不要调用演示bootstrap；当前运行脚本是本地演示入口。

## 测试

```sh
.venv/bin/python -m pytest -q
```

21项SQLite测试覆盖库存事务、去重、并发、额度保护、权限和回滚。浏览器人工验收记录位于开发说明。

## 技术结构

```text
app/
  main.py        FastAPI路由、校验、会话、权限及静态资源
  db.py          SQLAlchemy模型及事务管理
  domain.py      收货/交货领域事务、库存和额度约束
  security.py    密码哈希与会话token摘要
  bootstrap.py   显式初始化本地演示资料
  static/        响应式工作台，无CDN和前端构建依赖
tests/           回归与并发测试
docs/            阶段范围、接入契约、OpenAPI和PostgreSQL DDL
```

默认库存归属当前登录商户；多货主操作未实现。外部仓只读，Outbox只记待接入事件，不运行发送器。应用未暴露直接修改余额、删除流水接口。

数据库URL可通过 `DATABASE_URL` 配置成 `postgresql+psycopg://...`，驱动已安装，DDL见 `docs/schema-postgresql.sql`。**PostgreSQL分支尚未实测**，不能直接作为生产方案；后续需真实实例、版本化迁移和并发/恢复测试。

收货/交货超时后前端保留原请求并使用相同幂等键重试。请勿在不同浏览器或新表单中重新新建同一业务。该机制处理传输重试；识别重复业务单号与采购单收货累计将在下一阶段实现。
