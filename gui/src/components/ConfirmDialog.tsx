type ConfirmDialogProps = {
  title: string;
  message: string;
  details?: Record<string, unknown>;
  confirmLabel?: string;
  onConfirm(): void;
  onCancel(): void;
};

export function ConfirmDialog({ title, message, details, confirmLabel = "Confirm", onConfirm, onCancel }: ConfirmDialogProps) {
  return (
    <div className="modalBackdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <h2 id="confirm-title">{title}</h2>
        <p>{message}</p>
        {details ? <pre className="preview">{JSON.stringify(details, null, 2)}</pre> : null}
        <div className="modalActions">
          <button className="secondary" onClick={onCancel}>Cancel</button>
          <button className="danger" onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
