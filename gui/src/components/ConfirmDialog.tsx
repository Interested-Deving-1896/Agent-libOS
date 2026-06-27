import { CollapsibleJson } from "./CollapsibleJson";
import { useI18n } from "../i18n";

type ConfirmDialogProps = {
  title: string;
  message: string;
  details?: Record<string, unknown>;
  confirmLabel?: string;
  busy?: boolean;
  onConfirm(): void;
  onCancel(): void;
};

export function ConfirmDialog({ title, message, details, confirmLabel, busy = false, onConfirm, onCancel }: ConfirmDialogProps) {
  const { t } = useI18n();
  return (
    <div className="modalBackdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-busy={busy} aria-labelledby="confirm-title">
        <h2 id="confirm-title">{title}</h2>
        <p>{message}</p>
        {details ? <CollapsibleJson value={details} label={t("confirm.preview")} /> : null}
        <div className="modalActions">
          <button className="secondary" disabled={busy} onClick={onCancel}>{t("confirm.cancel")}</button>
          <button className="danger" disabled={busy} onClick={onConfirm}>{confirmLabel ?? t("confirm.confirm")}</button>
        </div>
      </div>
    </div>
  );
}
