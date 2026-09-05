/**
 * TopicTagList (FE §6.12) — plain labeled tag list.
 *
 * Topics carry NO citations (per the documented schema) — simple chips.
 */

export function TopicTagList({ topics }: { topics: string[] }) {
  return (
    <section className="summary-section">
      <h2 className="summary-section-title">Topics</h2>
      {topics.length === 0 ? (
        <p className="summary-empty">No topics identified in this document.</p>
      ) : (
        <div className="summary-topics">
          {topics.map((topic) => (
            <span key={topic} className="summary-topic-tag">
              {topic}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
