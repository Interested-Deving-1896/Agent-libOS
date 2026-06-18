import { CollapsibleJson } from "./CollapsibleJson";
import { useI18n } from "../i18n";

type ConfirmDialogProps = {
  title: string;
  message: string;
  details?: Record<string, unknown>;
  confirmLabel?: string;
  onConfirm(): void;
  onCancel(): void;
};

export function ConfirmDialog({ title, message, details, confirmLabel, onConfirm, onCancel }: ConfirmDialogProps) {
  const { t } = useI18n();
  return (
    <div className="modalBackdrop" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <h2 id="confirm-title">{title}</h2>
        <p>{message}</p>
        {details ? <CollapsibleJson value={details} label={t("confirm.preview")} /> : null}
        <div className="modalActions">
          <button className="secondary" onClick={onCancel}>{t("confirm.cancel")}</button>
          <button className="danger" onClick={onConfirm}>{confirmLabel ?? t("confirm.confirm")}</button>
        </div>
      </div>
    </div>
  );
}
