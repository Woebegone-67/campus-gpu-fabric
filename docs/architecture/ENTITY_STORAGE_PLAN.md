# 实体事务存储：实施协议

状态：实施前冻结方案，尚无新性能结果。基于账户交接 `7c71b3e`，分支 `work/storage-architecture`。

目标是消除普通事务对全部已完成历史的读取、复制、序列化、比较与重写。保留全部历史、WAL/FULL、跨进程SQLite单写事务、原子资源/事件/回执/outbox，以及独立逐提交重放。不得改官方API或降低CP4门槛。

## 存储接口

- `transaction()` 仍产生独立SQLite连接，BEGIN IMMEDIATE；无跨事务可变缓存。
- `load(conn)` 返回本事务私有root dict，标量直接加载，顶层map与events/list采用懒加载容器。容器按键读单行，嵌套对象在本事务内可原地改；save比较已触及行的原始规范JSON，不能只记录顶层set。
- map提供 `values_where(**filters)`：字段固定白名单，scalar为相等、tuple为IN；结果覆盖本事务内新建/修改/删除，不能只查数据库旧值。另有 `touched_values()`，包含本事务触及的现存对象，供校验。
- `snapshot()` 是BEGIN只读事务的上下文，提供同样懒加载视图；lazy值不得逃出关闭连接。`read()` 继续返回完整普通dict/list，作为审计/兼容接口。
- save对普通完整dict也保留支持，用于迁移/测试/导入；该慢路径允许全量。显式reset/完整导出/启动审计也允许处理全部历史。普通命令不得走这些慢路径。

## 规范化单元

root必须是JSON对象。每个单元的key为规范JSON数组字符串：`[顶层字段]`或`[顶层字段,实体键/列表整数下标]`。顶层标记值分别为`["map"]`、`["list",长度]`、`["scalar",原值]`；第二层单元值为完整实体/元素JSON，不再拆其内部嵌套结构。空map/list也有标记，缺失/null/bool/int/float/负零不可混淆。任意未知顶层字段也保存，禁止只保存白名单业务字段。

单元表与查询索引是同一事务内的权威数据；不能建立另一个可漂移的空闲资源账本。实体过滤索引覆盖state、owner_id、allocation_id、intent_id、status、physical_live、pending_terminal，必要时增加明确字段，但不能根据终态删除历史。

## 增量状态根与日志

新增确定性Merkle treap，以单元key为BST顺序，以SHA256(key)及key为确定性优先级。节点摘要绑定key、该单元规范JSON摘要、左右子树摘要；空树固定域分离摘要。任何单元新增/修改/删除都更新路径根，在线成本随受影响单元与树高变化，不重新哈希所有历史。节点与单元、journal、head必须同事务提交。

存储接口：`MerkleTree(conn, root_key=None)`，`set(key, payload_text)`、`delete(key)`，以及`root_key`、`root_hash`；`create_schema(conn)`建立`fabric_tree`。根摘要由完整单元覆盖决定，不是把journal链头冒称状态根。

原journal v1字节与顺序保留。新记录schema为`fabric-committed-state-v2`，仍用sequence、origin、previous_record_sha256、record_sha256；用before_root/after_root替代在线完整JSON SHA。changes是单元级`{op:"set",key:规范key字符串,value:单元值}`或`{op:"delete",key:规范key字符串}`。新数据库initialize记录before_root为null，完整初值单元进入changes。

导出schema v2允许v1历史前缀与v2尾部，保留原sequence和record hash，不伪造迁移前历史。最终导出包含head_sequence/head_record_sha256、state_root和完整当前JSON的state_sha256；最终完整hash在离线导出时计算。v1→v2的第一条记录before_root必须等于独立重建旧前缀状态的根。schema1仅有状态时保留显式v1 migration-baseline，不宣称有更早历史。

独立reducer只用标准库，不导入生产存储/哈希树/变更生成器；支持v1与混合v2，校验每条根与链，并继续按独立SQL重组状态的canonical SHA比较提交前缀。不能只比较生产代码自己返回的root。自洽重签不能替代外部固定摘要或独立观测。

## 实施与验证顺序

先落实存储垂直切片、迁移与独立重放，再将内核热循环替换为用途明确的实体选择集；只校验触及的intent。去掉deepcopy时必须捕获原始version/now，拒绝不得返回被递增的版本，且拒绝不save。

保留keep_lease终态工作仍占资源、Preempted/恢复Pending的原TTL、暂停job超时、真实停止未确认隔离、reset旧代屏障、delivered outbox的代次去重。时钟大步/分步、同时事件排序不得改变。

验证：迁移并发与重开、每种单元类型和任意字段、删除/reset、嵌套原地修改、拒绝无副作用、提交两侧SIGKILL、32并发/多进程、回执幂等、逐提交SQL观测与重放、日志/树/实体篡改负例。然后固定活动对象，增加终态历史，测量加载行数、写入行数与真实事务时间；最后才决定是否具备运行完整CP4负载的条件。诊断不等于验收通过。
