/** Session list sidebar showing past and active ML sessions. */
export default function SessionSidebar() {
  const sessions = [
    { id: "1", label: "新 ML 任务", active: true },
    { id: "2", label: "超参数搜索", active: false },
  ];

  return (
    <nav className="session-sidebar">
      <div className="session-sidebar__header">
        <span className="session-sidebar__title">会话</span>
      </div>
      <ul className="session-sidebar__list">
        {sessions.map((s) => (
          <li key={s.id} className={`session-sidebar__item${s.active ? " session-sidebar__item--active" : ""}`}>
            <span className="session-sidebar__label">{s.label}</span>
          </li>
        ))}
      </ul>
      <div className="session-sidebar__footer">
        <button className="session-sidebar__new">+ 新建</button>
      </div>
    </nav>
  );
}
