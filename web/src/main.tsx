import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const workspaces = [
  ['运行工作台', '从工作意图开始，连接申请、执行与结果。'],
  ['资源与贡献', '让共享、借用和收回都有明确归属。'],
  ['任务连续性', '同一项工作，跨越中断与恢复。'],
  ['决策解释', '从当时的约束理解实际选择。'],
  ['事件与实验', '在隔离运行中比较策略与个体得失。'],
  ['系统验收', '每个结论对应实际执行的证据。'],
];

function App() {
  const [active, setActive] = useState(0);
  const [connection, setConnection] = useState('正在连接开发服务');
  useEffect(() => {
    const abort = new AbortController();
    fetch('/healthz', { signal: abort.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error('Health request failed');
        const data = await res.json();
        if (data.checkpoint !== 'CP0') throw new Error('Unexpected service identity');
        setConnection('开发服务已连接 · CP0');
      })
      .catch((error) => {
        if (error.name !== 'AbortError') setConnection('开发服务未连接 · 请启动 make api');
      });
    return () => abort.abort();
  }, []);
  return (
    <div className="app">
      <aside>
        <div className="wordmark"><span>织算</span><small>CAMPUS FABRIC</small></div>
        <nav aria-label="工作区">{workspaces.map(([name], i) => (
          <button key={name} aria-current={active === i ? 'page' : undefined} onClick={() => setActive(i)}>
            <span className="number">0{i + 1}</span>{name}
          </button>
        ))}</nav>
        <footer>SHARED COMPUTE<br/>ACCOUNTABLE SERVICE</footer>
      </aside>
      <main>
        <header><span>开发工作区</span><span role="status">{connection}</span></header>
        <section className="intro"><p className="eyebrow">{String(active + 1).padStart(2, '0')} / WORKSPACE</p><h1>{workspaces[active][0]}</h1><p>{workspaces[active][1]}</p></section>
        <section className="notice">
          <span className="tag">CP0 · 契约与项目骨架</span>
          <h2>正式开发的共同起点</h2>
          <p>业务账本与运行数据尚未接入。这里没有模拟任务或预设成功结果；后续状态将来自同一权威服务。</p>
          <dl><div><dt>A · 可信资源与执行</dt><dd>Xiuzhu Du / Woebegone-67</dd></div><div><dt>B · 服务决策与体验</dt><dd>xuyangma</dd></div></dl>
        </section>
        <section className="next"><span>下一交汇点</span><strong>CP1 · 正常旅程接通</strong><p>真实提交、资源租约、任务结算与部分成功处理。</p></section>
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
