import type { ImageSummary } from "../api/types";
import { useI18n } from "../i18n";

type ImageSelectProps = {
  images: ImageSummary[];
  value: string;
  label?: string;
  onChange(value: string): void;
};

export function ImageSelect({ images, value, label, onChange }: ImageSelectProps) {
  const { t } = useI18n();
  const known = images.some((image) => image.image_id === value);
  return (
    <label className="imageSelect">
      <span>{label ?? t("image.selectLabel")}</span>
      <select value={known ? value : ""} onChange={(event) => onChange(event.currentTarget.value)}>
        {!known ? <option value="">{t("image.customOption")}</option> : null}
        {images.map((image) => (
          <option key={image.image_id} value={image.image_id}>
            {image.image_id} · {image.boot_kind}
          </option>
        ))}
      </select>
      <input value={value} onChange={(event) => onChange(event.currentTarget.value)} placeholder={t("image.manualPlaceholder")} />
    </label>
  );
}
