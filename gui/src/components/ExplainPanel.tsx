import { useEffect, useMemo, useState } from "react";
import type { ExplainOperationResponse, OperationEvidence, OperationListResponse, OperationSummary } from "../api/types";
import { useI18n } from "../i18n";
import { CollapsibleJson } from "./CollapsibleJson";

export function ExplainPanel({
  pid,
  listOperations,
  explainOperation,
  resolveOperation,
  lookup
}: {
  pid: string;
  listOperations(pid: string, cursor?: string): Promise<OperationListResponse>;
  explainOperation(operationId: string, cursor?: string): Promise<ExplainOperationResponse>;
  resolveOperation(kind: string, id: string): Promise<ExplainOperationResponse>;
  lookup: { kind: string; id: string; nonce: number } | null;
}) {
  const { formatTime, t } = useI18n();
  const [operations, setOperations] = useState<OperationSummary[]>([]);
  const [operationCursor, setOperationCursor] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [explanation, setExplanation] = useState<ExplainOperationResponse | null>(null);
  const [evidenceType, setEvidenceType] = useState("all");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setOperations([]);
    setExplanation(null);
    setSelectedId(null);
    setError(null);
    setBusy(true);
    void listOperations(pid).then(async (response) => {
      if (cancelled) return;
      setOperations(response.operations);
      setOperationCursor(response.next_cursor);
      const first = response.operations[0];
      if (lookup || !first) return;
      const detail = await explainOperation(first.operation_id);
      if (!cancelled) {
        setSelectedId(detail.selected_operation_id);
        setExplanation(detail);
      }
    }).catch((reason) => {
      if (!cancelled) setError(String(reason));
    }).finally(() => {
      if (!cancelled) setBusy(false);
    });
    return () => { cancelled = true; };
  }, [pid]);

  useEffect(() => {
    if (!lookup) return;
    let cancelled = false;
    setBusy(true);
    void resolveOperation(lookup.kind, lookup.id).then((detail) => {
      if (cancelled) return;
      setSelectedId(detail.selected_operation_id);
      setExplanation(detail);
      setEvidenceType("all");
    }).catch((reason) => {
      if (!cancelled) setError(String(reason));
    }).finally(() => {
      if (!cancelled) setBusy(false);
    });
    return () => { cancelled = true; };
  }, [lookup?.nonce]);

  const evidenceTypes = useMemo(
    () => ["all", ...Array.from(new Set((explanation?.evidence ?? []).map((item) => item.evidence_type))).sort()],
    [explanation]
  );
  const visibleEvidence = useMemo(
    () => filterOperationEvidence(explanation?.evidence ?? [], evidenceType),
    [evidenceType, explanation]
  );

  async function select(operationId: string) {
    setBusy(true);
    setError(null);
    try {
      setSelectedId(operationId);
      setExplanation(await explainOperation(operationId));
      setEvidenceType("all");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function loadMoreOperations() {
    if (!operationCursor) return;
    const response = await listOperations(pid, operationCursor);
    setOperations((current) => [...current, ...response.operations]);
    setOperationCursor(response.next_cursor);
  }

  async function loadMoreEvidence() {
    if (!explanation?.next_cursor) return;
    const next = await explainOperation(explanation.selected_operation_id, explanation.next_cursor);
    setExplanation(mergeEvidencePage(explanation, next));
  }

  if (busy && operations.length === 0) return <div className="empty">{t("explain.loading")}</div>;
  if (error) return <div className="empty explainError">{error}</div>;
  if (operations.length === 0) return <div className="empty">{t("explain.empty")}</div>;

  return (
    <div className="explainPanel">
      <section className="explainOperationList">
        <h3>{t("explain.operations")}</h3>
        {operations.map((operation) => (
          <button
            type="button"
            className={selectedId === operation.operation_id ? "active" : ""}
            key={operation.operation_id}
            onClick={() => void select(operation.operation_id)}
          >
            <strong>{operation.name}</strong>
            <span>{operation.outcome} · {formatTime(operation.started_at)}</span>
          </button>
        ))}
        {operationCursor ? <button type="button" onClick={() => void loadMoreOperations()}>{t("explain.loadMore")}</button> : null}
      </section>

      {explanation ? (
        <>
          <section className={`explainSummary ${explanation.summary.outcome}`}>
            <h3>{explanation.summary.headline}</h3>
            <div className="explainBadges">
              <span>{t("explain.outcome")}: {explanation.summary.outcome}</span>
              <span>{t("explain.complete")}: {explanation.evidence_complete ? t("common.yes") : t("common.no")}</span>
              <span>{t("explain.effects")}: {explanation.summary.external_effects.length}</span>
              <span>{t("explain.resourceCharges")}: {explanation.summary.resource_charge_count}</span>
            </div>
            {explanation.missing_evidence.length ? <CollapsibleJson value={{ missing_evidence: explanation.missing_evidence }} /> : null}
            {explanation.uncertainties.length ? <CollapsibleJson value={{ uncertainties: explanation.uncertainties }} /> : null}
            <CollapsibleJson value={{
              authorization: explanation.summary.authorization,
              human: explanation.summary.human,
              external_effects: explanation.summary.external_effects,
              resource_consumption: explanation.summary.resource_consumption,
              context: explanation.summary.context
            }} />
          </section>

          <section className="explainTree">
            <h3>{t("explain.causalTree")}</h3>
            <OperationTree operations={explanation.operations} rootId={explanation.root.operation_id} selectedId={selectedId} onSelect={select} />
          </section>

          <section className="explainEvidence">
            <div className="explainEvidenceHeader">
              <h3>{t("explain.timeline")}</h3>
              <select value={evidenceType} onChange={(event) => setEvidenceType(event.currentTarget.value)}>
                {evidenceTypes.map((value) => <option value={value} key={value}>{value}</option>)}
              </select>
            </div>
            {visibleEvidence.map((item) => <EvidenceRow key={`${item.evidence_type}:${item.evidence_id}`} item={item} />)}
            {explanation.next_cursor ? <button type="button" onClick={() => void loadMoreEvidence()}>{t("explain.loadMore")}</button> : null}
          </section>
        </>
      ) : null}
    </div>
  );
}

function OperationTree({
  operations,
  rootId,
  selectedId,
  onSelect
}: {
  operations: OperationSummary[];
  rootId: string;
  selectedId: string | null;
  onSelect(operationId: string): Promise<void>;
}) {
  const root = operations.find((item) => item.operation_id === rootId);
  if (!root) return null;
  const children = buildOperationChildren(operations);
  const render = (item: OperationSummary) => (
    <li key={item.operation_id}>
      <button type="button" className={selectedId === item.operation_id ? "active" : ""} onClick={() => void onSelect(item.operation_id)}>
        {item.kind} · {item.name} · {item.outcome}
      </button>
      {(children.get(item.operation_id) ?? []).length ? <ul>{(children.get(item.operation_id) ?? []).map(render)}</ul> : null}
    </li>
  );
  return <ul className="operationTree">{render(root)}</ul>;
}

export function buildOperationChildren(operations: OperationSummary[]): Map<string, OperationSummary[]> {
  const children = new Map<string, OperationSummary[]>();
  for (const item of operations) {
    if (!item.parent_operation_id) continue;
    children.set(item.parent_operation_id, [...(children.get(item.parent_operation_id) ?? []), item]);
  }
  return children;
}

export function filterOperationEvidence(items: OperationEvidence[], evidenceType: string): OperationEvidence[] {
  return items.filter((item) => evidenceType === "all" || item.evidence_type === evidenceType);
}

export function mergeEvidencePage(
  current: ExplainOperationResponse,
  next: ExplainOperationResponse
): ExplainOperationResponse {
  const evidence = new Map<string, OperationEvidence>();
  for (const item of [...current.evidence, ...next.evidence]) {
    const key = `${item.evidence_type}:${item.evidence_id}`;
    const existing = evidence.get(key);
    evidence.set(key, existing ? {
      ...existing,
      ...item,
      roles: Array.from(new Set([...existing.roles, ...item.roles])).sort()
    } : item);
  }
  return {
    ...next,
    evidence: Array.from(evidence.values())
  };
}

function EvidenceRow({ item }: { item: OperationEvidence }) {
  const { formatTime } = useI18n();
  return (
    <article className="explainEvidenceRow">
      <header>
        <strong>{item.evidence_type}</strong>
        <span>{item.roles.join(", ")}</span>
        <time>{item.occurred_at ? formatTime(item.occurred_at) : "—"}</time>
      </header>
      <code>{item.evidence_id}</code>
      <CollapsibleJson value={item.data} />
    </article>
  );
}
