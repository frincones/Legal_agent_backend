import { motion } from 'motion/react';
import { X, Clock, Check, Download, Search, Scale, Brain, ExternalLink, ChevronDown, ChevronRight } from 'lucide-react';
import { Button } from './ui/button';
import { useState } from 'react';
import type { ThinkingStep } from './AgentThinking';

export interface VigenciaItem {
  tipo: string;
  numero?: number;
  anio?: number;
  estado: string;
  titulo?: string;
  derogada_por?: string;
}

export interface SourceRef {
  url: string;
  title: string;
  source: string;
  preview: string;
}

interface ActivityPanelProps {
  isOpen: boolean;
  onClose: () => void;
  steps: ThinkingStep[];
  duration?: number;
  vigencia?: VigenciaItem[];
  sources?: string[];
  sourceRefs?: SourceRef[];
  jurisprudencia?: Record<string, unknown>[];
  caseIndex?: number;
  turnInCase?: number;
}

function getStepIcon(step: ThinkingStep) {
  if (step.type === 'ingest') return <Download className="w-3.5 h-3.5 text-blue-500" />;
  const t = step.text.toLowerCase();
  if (t.includes('busca') || t.includes('consult')) return <Search className="w-3.5 h-3.5 text-amber-500" />;
  if (t.includes('vigencia')) return <Scale className="w-3.5 h-3.5 text-purple-500" />;
  if (t.includes('genera')) return <Brain className="w-3.5 h-3.5 text-pink-500" />;
  return <Check className="w-3.5 h-3.5 text-green-500" />;
}

export default function ActivityPanel({ isOpen, onClose, steps, duration, vigencia, sources, sourceRefs, jurisprudencia, caseIndex, turnInCase }: ActivityPanelProps) {
  // Status pasos collapsed by default — they're high-volume and look
  // identical between turns (the actual differentiation is in vigencia,
  // jurisprudencia, ingest and sources). High-value sections expanded.
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set(['vigencia', 'jurisprudencia', 'ingest']));

  if (!isOpen) return null;

  const toggle = (key: string) => {
    setExpandedSections(prev => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  };

  const ingestSteps = steps.filter(s => s.type === 'ingest');
  const statusSteps = steps.filter(s => s.type === 'status');

  return (
    <>
      {/* Backdrop mobile */}
      <div className="fixed inset-0 bg-black/20 z-40 md:hidden" onClick={onClose} />

      <motion.div
        initial={{ x: '100%' }}
        animate={{ x: 0 }}
        exit={{ x: '100%' }}
        transition={{ type: 'spring', damping: 30, stiffness: 300 }}
        className="fixed right-0 top-0 h-screen w-full max-w-sm bg-background border-l border-border z-50 flex flex-col shadow-xl overflow-hidden
                   md:w-80 md:max-w-none"
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-border">
          <div className="flex items-center gap-2 flex-wrap">
            <Clock className="w-4 h-4 text-muted-foreground" />
            <span className="text-sm font-semibold">Actividad</span>
            {(caseIndex !== undefined && (caseIndex > 1 || turnInCase !== undefined)) && (
              <span
                className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400"
                title={caseIndex > 1 ? `Sesión con múltiples casos. Este turno pertenece al caso ${caseIndex}.` : `Turno ${turnInCase} del caso actual.`}
              >
                Caso {caseIndex}{turnInCase !== undefined ? ` · Turno ${turnInCase}` : ''}
              </span>
            )}
            {duration !== undefined && (
              <span className="text-xs text-muted-foreground bg-muted px-1.5 py-0.5 rounded">{duration}s</span>
            )}
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} className="h-7 w-7 p-0">
            <X className="w-4 h-4" />
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto">
          <div className="p-4 space-y-4">

            {/* High-value, turn-specific data first. Status pasos move
                to the bottom (collapsed) because they read identical
                across turns and dilute the visual signal. */}

            {/* Downloads */}
            {ingestSteps.length > 0 && (
              <div>
                <button onClick={() => toggle('ingest')} className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2 hover:text-foreground transition-colors">
                  {expandedSections.has('ingest') ? <ChevronDown className="w-3 h-3"/> : <ChevronRight className="w-3 h-3"/>}
                  Normativa descargada ({ingestSteps.length})
                </button>
                {expandedSections.has('ingest') && (
                  <div className="space-y-1 ml-1">
                    {ingestSteps.map((step) => (
                      <div key={step.id} className="flex items-center gap-2 py-1">
                        <Download className="w-3 h-3 text-blue-500 flex-shrink-0" />
                        <span className="text-xs text-blue-600 dark:text-blue-400">{step.text}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Vigencia */}
            {vigencia && vigencia.length > 0 && (
              <div>
                <button onClick={() => toggle('vigencia')} className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2 hover:text-foreground transition-colors">
                  {expandedSections.has('vigencia') ? <ChevronDown className="w-3 h-3"/> : <ChevronRight className="w-3 h-3"/>}
                  Vigencia verificada ({vigencia.length})
                </button>
                {expandedSections.has('vigencia') && (
                  <div className="space-y-1.5 ml-1">
                    {vigencia.map((v, i) => (
                      <div key={i} className={`flex items-start gap-2 py-1 px-2 rounded text-xs ${
                        v.estado === 'VIGENTE' ? 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-400' :
                        v.estado === 'DEROGADA' ? 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400' :
                        'bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400'
                      }`}>
                        <span className="flex-shrink-0 mt-0.5">
                          {v.estado === 'VIGENTE' ? '✅' : v.estado === 'DEROGADA' ? '❌' : '⚠️'}
                        </span>
                        <div className="min-w-0">
                          <div className="font-medium">{v.tipo} {v.numero} de {v.anio}</div>
                          {v.estado === 'DEROGADA' && v.derogada_por && (
                            <div className="text-[10px] opacity-80">Derogada por {v.derogada_por}</div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Jurisprudencia cited */}
            {jurisprudencia && jurisprudencia.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-xs font-semibold text-foreground">Jurisprudencia citada</span>
                  <span className="text-[10px] text-muted-foreground">{jurisprudencia.length}</span>
                </div>
                <div className="space-y-1.5">
                  {jurisprudencia.map((j, i) => (
                    <div key={i} className="p-2 rounded-lg bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800">
                      <div className="text-xs font-medium text-purple-700 dark:text-purple-400">
                        {String(j.titulo || '')}
                      </div>
                      {j.source && (
                        <div className="text-[10px] text-purple-600/70 dark:text-purple-400/70 mt-0.5">
                          {String(j.source)}
                        </div>
                      )}
                      {j.preview && (
                        <div className="text-[10px] text-muted-foreground mt-0.5 line-clamp-2">
                          {String(j.preview)}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Sources — Copilot-style cards with URL, title, preview */}
            {sourceRefs && sourceRefs.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-3">
                  <span className="text-xs font-semibold text-foreground">Fuentes</span>
                  <span className="text-[10px] text-muted-foreground">{sourceRefs.length}</span>
                </div>
                <div className="space-y-2">
                  {sourceRefs.map((ref, i) => {
                    const domain = ref.url ? new URL(ref.url).hostname.replace('www.', '') : ref.source || '';
                    return (
                      <a
                        key={i}
                        href={ref.url || '#'}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="block p-2.5 rounded-lg border border-border/50 hover:border-border hover:bg-muted/30 transition-all group"
                      >
                        <div className="flex items-start gap-2">
                          {/* Domain icon */}
                          <div className="flex-shrink-0 w-5 h-5 rounded bg-muted flex items-center justify-center mt-0.5">
                            <span className="text-[8px] font-bold text-muted-foreground uppercase">
                              {domain.slice(0, 2)}
                            </span>
                          </div>
                          <div className="flex-1 min-w-0">
                            {/* Domain */}
                            <div className="text-[10px] text-muted-foreground truncate">{domain}</div>
                            {/* Title */}
                            <div className="text-xs font-medium text-foreground leading-tight mt-0.5 line-clamp-2">
                              {ref.title}
                            </div>
                            {/* Preview */}
                            {ref.preview && (
                              <div className="text-[10px] text-muted-foreground/70 leading-tight mt-1 line-clamp-2">
                                {ref.preview}
                              </div>
                            )}
                          </div>
                          <ExternalLink className="w-3 h-3 text-muted-foreground/30 group-hover:text-muted-foreground flex-shrink-0 mt-1" />
                        </div>
                      </a>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Fallback: simple source list when no sourceRefs */}
            {(!sourceRefs || sourceRefs.length === 0) && sources && sources.length > 0 && (
              <div>
                <button onClick={() => toggle('sources')} className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2 hover:text-foreground transition-colors">
                  {expandedSections.has('sources') ? <ChevronDown className="w-3 h-3"/> : <ChevronRight className="w-3 h-3"/>}
                  Fuentes consultadas ({sources.length})
                </button>
                {expandedSections.has('sources') && (
                  <div className="space-y-1 ml-1">
                    {sources.map((s, i) => (
                      <div key={i} className="flex items-center gap-2 py-0.5">
                        <ExternalLink className="w-3 h-3 text-muted-foreground flex-shrink-0" />
                        <span className="text-xs text-foreground/70 truncate">{s}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Thinking Steps — collapsed by default, low signal */}
            {statusSteps.length > 0 && (
              <div>
                <button onClick={() => toggle('steps')} className="flex items-center gap-1 text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2 hover:text-foreground transition-colors">
                  {expandedSections.has('steps') ? <ChevronDown className="w-3 h-3"/> : <ChevronRight className="w-3 h-3"/>}
                  Pensamiento ({statusSteps.length} pasos)
                </button>
                {expandedSections.has('steps') && (
                  <div className="space-y-1 ml-1">
                    {statusSteps.map((step) => (
                      <div key={step.id} className="flex items-start gap-2 py-1">
                        <div className="mt-0.5 flex-shrink-0">{getStepIcon(step)}</div>
                        <span className="text-xs text-foreground/80 leading-relaxed">{step.text}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Duration */}
            {duration !== undefined && (
              <div className="pt-2 border-t border-border">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Clock className="w-3.5 h-3.5" />
                  <span>Penso durante {duration}s</span>
                  <span className="text-green-500 ml-auto">Listo</span>
                </div>
              </div>
            )}
          </div>
        </div>
      </motion.div>
    </>
  );
}
