# 义乌商贸库存管家

自建本地开发版本 v0.3.0：在采购、导入和审批基础上，新增仓间调拨、退货质检、岗位账号管理及备份恢复演练。

当前为**本地开发版本**，演示商户与库存不是实际业务数据；未接入平台和海外仓。当前功能、验收及边界见 [第三阶段4—6项交付说明](docs/第三阶段_4-6项交付说明.md)；[第二阶段1—3项交付说明](docs/第二阶段_1-3项交付说明.md)；[第一阶段开发说明](docs/第一阶段开发说明.md)作为历史交付记录保留。

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

不要将此版本开放到公网或录入真实客户隐私。生产认证、容量压测、自动异地备份和真实连接器仍需后续实施。本地启动自动执行已登记的数据库升级；升级前请备份数据库，切勿直接改已应用的迁移。创建新的生产数据库时不要调用演示bootstrap；当前运行脚本是本地演示入口。

## 测试

```sh
.venv/bin/python -m pytest -q
```

69项SQLite测试通过；PostgreSQL测试模式68项通过、1项SQLite专用测试跳过。覆盖调拨、退货质检、账号权限、迁移、恢复及前两阶段回归。浏览器验收记录见第三阶段交付说明。

## 技术结构

```text
app/
  main.py        FastAPI路由、校验、会话、权限及静态资源
  migrations.py  有序版本迁移（已有数据库升级）
  workflows.py   导入、采购、审批与冲正领域事务
  phase2.py      第二阶段请求模型及接口
  operations.py  调拨、退货及质检事务
  accounts.py    账号权限与管理审计
  backup.py      SQLite在线备份及独立恢复
  phase3.py      第三阶段请求模型及接口
  db.py          SQLAlchemy模型及事务管理
  domain.py      收货/交货领域事务、库存和额度约束
  security.py    密码哈希与会话token摘要
  bootstrap.py   显式初始化本地演示资料
  static/        响应式工作台，无CDN和前端构建依赖
tests/           回归与并发测试
docs/            阶段范围、接入契约、OpenAPI和PostgreSQL DDL
```

默认库存归属当前登录商户；多货主操作未实现。外部仓只读，Outbox只记待接入事件，不运行发送器。应用未暴露直接修改余额、删除流水接口。

数据库URL可通过 `DATABASE_URL` 配置成 `postgresql+psycopg://...`，驱动已安装，DDL见 `docs/schema-postgresql.sql`。已在本机隔离PostgreSQL 16.2实例完成迁移、并发及pg_dump/pg_restore演练，24张非会话表哈希一致；生产基础设施与容量尚待验证。

收货/交货超时后前端保留原请求并使用相同幂等键重试。请勿在不同浏览器或新表单中重新新建同一业务。该机制处理传输重试。采购收货另以采购单+到货凭证号防止重复业务，并限制累计实收不超过采购量；第一阶段普通无采购单收货仍不具备业务凭证去重。

## 第二阶段操作入口

- 批量导入：管理员先下载模板，填写UTF-8 CSV并预览，通过后确认整批导入。期初仅适用于无库存历史的仓库商品组合。
- 采购与收货：仓管或管理员新建多商品采购单，再用唯一到货凭证分批登记良品/待验/残次。
- 调整与冲正：使用warehouse申请，再切换admin审核；申请人不能审批自己的申请。
- 迁移与审批历史随数据库保存；本地账号密码、数据库和备份均不提交Git。

## 第三阶段操作入口

- 仓间调拨：预占→发出→分批签收；在途短少由另一管理员审核。
- 退货与质检：客户实物退回先待验，质检后分流；供应商退货关联原收货出库。
- 账号与试运行：岗位/仓库授权、停用撤销会话、库存约束检查、SQLite备份及独立恢复。
- 仍未实现自然语言Agent和外部平台连接器。

## PostgreSQL验证

准备独立测试实例，将SQLAlchemy连接URL保存在本机0600权限文件中，勿提交Git。测试账号需创建schema权限，恢复演练额外需要创建数据库权限。仅在测试实例运行：

```sh
TEST_DATABASE_URL_FILE=.local/pg-trial-url .venv/bin/python -m pytest -q --tb=no
.venv/bin/python scripts/postgres_verify.py --url-file .local/pg-trial-url --bin-dir /path/to/postgresql/bin
```

提供与服务端兼容的pg_dump/pg_restore即可；可选requirements-pg-test.txt包含本地隔离测试运行时。恢复报告、归档与凭据均留在.local中。
