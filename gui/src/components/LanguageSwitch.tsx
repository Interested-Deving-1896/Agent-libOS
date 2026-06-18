import { Languages } from "lucide-react";
import { useI18n, type Language } from "../i18n";

const options: Language[] = ["zh-CN", "en"];

export function LanguageSwitch() {
  const { language, setLanguage, t } = useI18n();
  return (
    <label className="languageSwitch" title={t("language.label")}>
      <Languages size={15} />
      <span className="srOnly">{t("language.label")}</span>
      <select value={language} onChange={(event) => setLanguage(event.currentTarget.value as Language)} aria-label={t("language.label")}>
        {options.map((option) => (
          <option key={option} value={option}>
            {option === "zh-CN" ? t("language.zh") : t("language.en")}
          </option>
        ))}
      </select>
    </label>
  );
}
