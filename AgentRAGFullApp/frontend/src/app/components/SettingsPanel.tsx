import { motion } from 'motion/react';
import { X, Brain, Sparkles, Database, Sliders } from 'lucide-react';
import { Button } from './ui/button';
import { Switch } from './ui/switch';
import { Separator } from './ui/separator';
import { ScrollArea } from './ui/scroll-area';
import { useState, useEffect } from 'react';
import { toast } from 'sonner';

interface SettingsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  darkMode?: boolean;
  onToggleDarkMode?: () => void;
}

interface AgentSettings {
  primaryModel: string;
  utilityModel: string;
  temperature: number;
  enableQueryExpansion: boolean;
  enableMultiQuery: boolean;
  enableReranking: boolean;
  enableSelfReflection: boolean;
  enableHybridSearch: boolean;
  enableConversationMemory: boolean;
  enableLegalSources: boolean;
  enableDatosGov: boolean;
  enableSenado: boolean;
  enableFuncionPublica: boolean;
  enableCorteCC: boolean;
  enableAutoVigencia: boolean;
}

const DEFAULT_SETTINGS: AgentSettings = {
  primaryModel: 'gpt-4o',
  utilityModel: 'gpt-4o-mini',
  temperature: 0.3,
  enableQueryExpansion: true,
  enableMultiQuery: true,
  enableReranking: true,
  enableSelfReflection: true,
  enableHybridSearch: true,
  enableConversationMemory: true,
  enableLegalSources: true,
  enableDatosGov: true,
  enableSenado: true,
  enableFuncionPublica: true,
  enableCorteCC: true,
  enableAutoVigencia: true,
};

export function SettingsPanel({ isOpen, onClose, darkMode, onToggleDarkMode }: SettingsPanelProps) {
  const [settings, setSettings] = useState<AgentSettings>(DEFAULT_SETTINGS);

  useEffect(() => {
    const saved = localStorage.getItem('agent-settings');
    if (saved) {
      try {
        setSettings({ ...DEFAULT_SETTINGS, ...JSON.parse(saved) });
      } catch {
        /* ignore */
      }
    }
  }, []);

  const updateSetting = <K extends keyof AgentSettings>(key: K, value: AgentSettings[K]) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = () => {
    localStorage.setItem('agent-settings', JSON.stringify(settings));
    toast.success('Configuración guardada');
    onClose();
  };

  if (!isOpen) return null;

  return (
    <>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        className="fixed inset-0 bg-black/50 backdrop-blur-sm z-40"
      />

      <motion.div
        initial={{ x: '100%' }}
        animate={{ x: 0 }}
        exit={{ x: '100%' }}
        transition={{ type: 'spring', damping: 30, stiffness: 300 }}
        className="fixed right-0 top-0 h-full w-96 bg-card border-l border-border shadow-2xl z-50 flex flex-col"
      >
        {/* Header */}
        <div className="p-6 border-b border-border">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-xl font-semibold">Configuración</h2>
              <p className="text-sm text-muted-foreground mt-1">
                Personaliza el comportamiento del agente
              </p>
            </div>
            <Button variant="ghost" size="icon" onClick={onClose}>
              <X className="w-5 h-5" />
            </Button>
          </div>
        </div>

        {/* Content */}
        <ScrollArea className="flex-1">
          <div className="p-6 space-y-8">
            {/* Models */}
            <Section icon={Brain} title="Modelos">
              <FieldRow label="Modelo principal">
                <select
                  value={settings.primaryModel}
                  onChange={(e) => updateSetting('primaryModel', e.target.value)}
                  className="text-xs bg-background border border-border rounded px-2 py-1"
                >
                  <option value="gpt-4o">gpt-4o</option>
                  <option value="gpt-4o-mini">gpt-4o-mini</option>
                  <option value="gpt-4-turbo">gpt-4-turbo</option>
                </select>
              </FieldRow>
              <FieldRow label="Modelo utilitario">
                <select
                  value={settings.utilityModel}
                  onChange={(e) => updateSetting('utilityModel', e.target.value)}
                  className="text-xs bg-background border border-border rounded px-2 py-1"
                >
                  <option value="gpt-4o-mini">gpt-4o-mini</option>
                  <option value="gpt-4o">gpt-4o</option>
                </select>
              </FieldRow>
              <FieldRow label={`Temperature: ${settings.temperature.toFixed(1)}`}>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.1}
                  value={settings.temperature}
                  onChange={(e) => updateSetting('temperature', parseFloat(e.target.value))}
                  className="w-32"
                />
              </FieldRow>
            </Section>

            <Separator />

            {/* RAG strategies */}
            <Section icon={Sparkles} title="Estrategias RAG">
              <FieldRow label="Query Expansion">
                <Switch
                  checked={settings.enableQueryExpansion}
                  onCheckedChange={(v) => updateSetting('enableQueryExpansion', v)}
                />
              </FieldRow>
              <FieldRow label="Multi-Query">
                <Switch
                  checked={settings.enableMultiQuery}
                  onCheckedChange={(v) => updateSetting('enableMultiQuery', v)}
                />
              </FieldRow>
              <FieldRow label="Re-ranking">
                <Switch
                  checked={settings.enableReranking}
                  onCheckedChange={(v) => updateSetting('enableReranking', v)}
                />
              </FieldRow>
              <FieldRow label="Self-Reflection">
                <Switch
                  checked={settings.enableSelfReflection}
                  onCheckedChange={(v) => updateSetting('enableSelfReflection', v)}
                />
              </FieldRow>
              <FieldRow label="Hybrid Search">
                <Switch
                  checked={settings.enableHybridSearch}
                  onCheckedChange={(v) => updateSetting('enableHybridSearch', v)}
                />
              </FieldRow>
            </Section>

            <Separator />

            {/* Memory */}
            <Section icon={Database} title="Memoria">
              <FieldRow label="Memoria conversacional">
                <Switch
                  checked={settings.enableConversationMemory}
                  onCheckedChange={(v) => updateSetting('enableConversationMemory', v)}
                />
              </FieldRow>
              <p className="text-[10px] text-muted-foreground ml-10">
                El agente recuerda conversaciones pasadas y las usa como contexto.
              </p>
            </Section>

            <Separator />

            {/* Legal Sources */}
            <Section icon={Database} title="Fuentes Legales">
              <FieldRow label="Fuentes legales activas">
                <Switch
                  checked={settings.enableLegalSources}
                  onCheckedChange={(v) => updateSetting('enableLegalSources', v)}
                />
              </FieldRow>
              {settings.enableLegalSources && (
                <>
                  <FieldRow label="datos.gov.co (SUIN)">
                    <Switch
                      checked={settings.enableDatosGov}
                      onCheckedChange={(v) => updateSetting('enableDatosGov', v)}
                    />
                  </FieldRow>
                  <FieldRow label="Secretaría del Senado">
                    <Switch
                      checked={settings.enableSenado}
                      onCheckedChange={(v) => updateSetting('enableSenado', v)}
                    />
                  </FieldRow>
                  <FieldRow label="Función Pública">
                    <Switch
                      checked={settings.enableFuncionPublica}
                      onCheckedChange={(v) => updateSetting('enableFuncionPublica', v)}
                    />
                  </FieldRow>
                  <FieldRow label="Corte Constitucional">
                    <Switch
                      checked={settings.enableCorteCC}
                      onCheckedChange={(v) => updateSetting('enableCorteCC', v)}
                    />
                  </FieldRow>
                  <FieldRow label="Verificar vigencia auto">
                    <Switch
                      checked={settings.enableAutoVigencia}
                      onCheckedChange={(v) => updateSetting('enableAutoVigencia', v)}
                    />
                  </FieldRow>
                  <p className="text-[10px] text-muted-foreground ml-10">
                    Consulta fuentes oficiales colombianas en cada pregunta legal y verifica si las normas siguen vigentes.
                  </p>
                </>
              )}
            </Section>

            <Separator />

            {/* Theme */}
            <Section icon={Sliders} title="Apariencia">
              <FieldRow label="Modo oscuro">
                <Switch checked={darkMode} onCheckedChange={onToggleDarkMode} />
              </FieldRow>
            </Section>
          </div>
        </ScrollArea>

        {/* Footer */}
        <div className="p-6 border-t border-border">
          <div className="flex gap-3">
            <Button variant="outline" className="flex-1" onClick={onClose}>
              Cancelar
            </Button>
            <Button className="flex-1" onClick={handleSave}>
              Guardar
            </Button>
          </div>
        </div>
      </motion.div>
    </>
  );
}

function Section({
  icon: Icon,
  title,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
          <Icon className="w-4 h-4 text-primary" />
        </div>
        <h3 className="font-medium text-sm">{title}</h3>
      </div>
      <div className="space-y-2 ml-10">{children}</div>
    </div>
  );
}

function FieldRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1">
      <label className="text-xs text-foreground/90">{label}</label>
      {children}
    </div>
  );
}
