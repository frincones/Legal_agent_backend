import { CheckCircle, XCircle, AlertTriangle, HelpCircle } from 'lucide-react';

interface VigenciaIndicatorProps {
  estado: string;
  badge?: string;
  compact?: boolean;
}

export default function VigenciaIndicator({ estado, badge, compact = false }: VigenciaIndicatorProps) {
  const normalized = estado?.toUpperCase() || '';

  if (compact) {
    if (normalized === 'VIGENTE') return <span className="inline-flex items-center gap-1 text-xs font-medium text-green-600 dark:text-green-400"><CheckCircle className="w-3 h-3" /> Vigente</span>;
    if (normalized === 'DEROGADA') return <span className="inline-flex items-center gap-1 text-xs font-medium text-red-600 dark:text-red-400"><XCircle className="w-3 h-3" /> Derogada</span>;
    if (normalized === 'MODIFICADA') return <span className="inline-flex items-center gap-1 text-xs font-medium text-amber-600 dark:text-amber-400"><AlertTriangle className="w-3 h-3" /> Modificada</span>;
    return <span className="inline-flex items-center gap-1 text-xs font-medium text-gray-500"><HelpCircle className="w-3 h-3" /> {estado || 'Desconocido'}</span>;
  }

  const config: Record<string, { icon: typeof CheckCircle; bg: string; text: string; label: string }> = {
    VIGENTE: { icon: CheckCircle, bg: 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800', text: 'text-green-700 dark:text-green-400', label: 'Vigente' },
    DEROGADA: { icon: XCircle, bg: 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800', text: 'text-red-700 dark:text-red-400', label: 'Derogada' },
    MODIFICADA: { icon: AlertTriangle, bg: 'bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800', text: 'text-amber-700 dark:text-amber-400', label: 'Modificada' },
  };

  const c = config[normalized] || { icon: HelpCircle, bg: 'bg-gray-50 dark:bg-gray-800 border-gray-200 dark:border-gray-700', text: 'text-gray-600 dark:text-gray-400', label: estado || 'Desconocido' };
  const Icon = c.icon;

  return (
    <div className={`flex items-start gap-2 p-2.5 rounded-lg border ${c.bg}`}>
      <Icon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${c.text}`} />
      <div className="min-w-0">
        <span className={`text-sm font-medium ${c.text}`}>{c.label}</span>
        {badge && <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{badge}</p>}
      </div>
    </div>
  );
}
