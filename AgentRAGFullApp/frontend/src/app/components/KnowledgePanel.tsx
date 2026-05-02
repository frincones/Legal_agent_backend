import { useState, useEffect, useRef, useCallback } from 'react';
import { motion } from 'motion/react';
import { X, Upload, Trash2, FileText, Loader2, CheckCircle2, AlertCircle, Clock } from 'lucide-react';
import { Button } from './ui/button';
import { ScrollArea } from './ui/scroll-area';
import { toast } from 'sonner';
import { listDocuments, ingestFiles, deleteDocument, type DocumentItem } from '../../lib/api';

interface KnowledgePanelProps {
  isOpen: boolean;
  onClose: () => void;
}

export function KnowledgePanel({ isOpen, onClose }: KnowledgePanelProps) {
  const [docs, setDocs] = useState<DocumentItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const previousStatusesRef = useRef<Map<string, string>>(new Map());

  const refresh = useCallback(async (showLoader = true) => {
    if (showLoader) setIsLoading(true);
    try {
      const data = await listDocuments();
      setDocs(data.documents);

      // Notify on status transitions: pending/processing → completed | failed
      const prev = previousStatusesRef.current;
      data.documents.forEach((d) => {
        const prevStatus = prev.get(d.id);
        if (prevStatus && prevStatus !== d.status) {
          if (d.status === 'completed') {
            toast.success(`✅ "${d.title}" listo (${d.chunk_count} chunks)`);
          } else if (d.status === 'failed') {
            toast.error(`❌ "${d.title}": ${d.ingestion_error || 'Error desconocido'}`);
          }
        }
        prev.set(d.id, d.status);
      });
      // Clean up entries for deleted docs
      const currentIds = new Set(data.documents.map((d) => d.id));
      Array.from(prev.keys()).forEach((id) => {
        if (!currentIds.has(id)) prev.delete(id);
      });
    } catch (e) {
      if (showLoader) toast.error(`No se pudo cargar: ${(e as Error).message}`);
    } finally {
      if (showLoader) setIsLoading(false);
    }
  }, []);

  // Initial load when panel opens
  useEffect(() => {
    if (isOpen) refresh();
  }, [isOpen, refresh]);

  // Auto-poll while there are documents in pending/processing state
  useEffect(() => {
    if (!isOpen) return;

    const hasInflight = docs.some(
      (d) => d.status === 'pending' || d.status === 'processing'
    );

    if (!hasInflight) return;

    const interval = setInterval(() => {
      refresh(false); // silent refresh
    }, 1500);

    return () => clearInterval(interval);
  }, [isOpen, docs, refresh]);

  const handleUpload = useCallback(
    async (files: FileList | File[]) => {
      const arr = Array.from(files);
      if (arr.length === 0) return;
      setIsUploading(true);
      try {
        const result = await ingestFiles(arr);

        if (result.queued > 0) {
          toast.success(
            `${result.queued}/${result.total} ${
              result.queued === 1 ? 'archivo en cola' : 'archivos en cola'
            }. Procesando en segundo plano...`
          );
        }

        if (result.failed > 0) {
          const errors = result.results
            .filter((r) => r.status === 'failed')
            .map((r) => `${r.file}: ${r.error}`)
            .join('\n');
          toast.error(`Errores:\n${errors}`);
        }

        // Refresh immediately to show the new pending docs
        refresh(false);
      } catch (e) {
        toast.error(`Error al subir: ${(e as Error).message}`);
      } finally {
        setIsUploading(false);
      }
    },
    [refresh]
  );

  const handleDelete = async (id: string) => {
    if (!confirm('¿Eliminar este documento del conocimiento?')) return;
    try {
      await deleteDocument(id);
      toast.success('Documento eliminado');
      refresh(false);
    } catch (e) {
      toast.error(`Error: ${(e as Error).message}`);
    }
  };

  if (!isOpen) return null;

  const inflightCount = docs.filter(
    (d) => d.status === 'pending' || d.status === 'processing'
  ).length;

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
        className="fixed right-0 top-0 h-full w-[420px] bg-card border-l border-border shadow-2xl z-50 flex flex-col"
      >
        {/* Header */}
        <div className="p-6 border-b border-border">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-xl font-semibold">Base de conocimiento</h2>
              <p className="text-sm text-muted-foreground mt-1">
                Documentos disponibles para el agente (RAG)
              </p>
            </div>
            <Button variant="ghost" size="icon" onClick={onClose}>
              <X className="w-5 h-5" />
            </Button>
          </div>
        </div>

        {/* Drop zone */}
        <div className="p-6">
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              if (e.dataTransfer.files) handleUpload(e.dataTransfer.files);
            }}
            onClick={() => fileInputRef.current?.click()}
            className={`
              border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-all
              ${dragOver
                ? 'border-primary bg-primary/5'
                : 'border-border hover:border-primary/50'
              }
            `}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                if (e.target.files) handleUpload(e.target.files);
              }}
            />
            {isUploading ? (
              <Loader2 className="w-8 h-8 mx-auto text-primary animate-spin" />
            ) : (
              <Upload className="w-8 h-8 mx-auto text-muted-foreground" />
            )}
            <p className="text-sm font-medium mt-3">
              {isUploading ? 'Subiendo...' : 'Arrastra archivos aquí o haz clic'}
            </p>
            <p className="text-[10px] text-muted-foreground mt-1">
              PDF, DOCX, XLSX, MD, TXT, código, audio, CSV...
            </p>
          </div>
        </div>

        {/* Document list header */}
        <div className="px-6 pb-2 flex items-center justify-between">
          <h3 className="text-xs font-medium text-muted-foreground flex items-center gap-2">
            DOCUMENTOS ({docs.length})
            {inflightCount > 0 && (
              <span className="text-[10px] font-normal text-primary flex items-center gap-1">
                <Loader2 className="w-2.5 h-2.5 animate-spin" />
                {inflightCount} procesando
              </span>
            )}
          </h3>
          <Button variant="ghost" size="sm" onClick={() => refresh()} className="text-xs h-7">
            Actualizar
          </Button>
        </div>

        <ScrollArea className="flex-1 px-6 pb-6">
          {isLoading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="w-5 h-5 animate-spin text-muted-foreground" />
            </div>
          ) : docs.length === 0 ? (
            <p className="text-center text-xs text-muted-foreground py-8">
              No hay documentos. Sube archivos para construir el conocimiento.
            </p>
          ) : (
            <div className="space-y-2">
              {docs.map((d) => (
                <DocRow key={d.id} doc={d} onDelete={() => handleDelete(d.id)} />
              ))}
            </div>
          )}
        </ScrollArea>
      </motion.div>
    </>
  );
}

function DocRow({ doc, onDelete }: { doc: DocumentItem; onDelete: () => void }) {
  const isInflight = doc.status === 'pending' || doc.status === 'processing';
  const isFailed = doc.status === 'failed';

  const StatusIcon = () => {
    if (doc.status === 'completed') {
      return <CheckCircle2 className="w-3 h-3 text-green-600" />;
    }
    if (doc.status === 'failed') {
      return <AlertCircle className="w-3 h-3 text-destructive" />;
    }
    if (doc.status === 'processing') {
      return <Loader2 className="w-3 h-3 text-primary animate-spin" />;
    }
    return <Clock className="w-3 h-3 text-muted-foreground" />;
  };

  const statusLabel = (() => {
    switch (doc.status) {
      case 'pending':
        return 'En cola';
      case 'processing':
        return 'Procesando...';
      case 'completed':
        return `${doc.chunk_count} chunks`;
      case 'failed':
        return 'Error';
      default:
        return doc.status;
    }
  })();

  return (
    <div
      className={`
        group flex items-start gap-3 p-3 rounded-lg border transition-colors
        ${isFailed
          ? 'border-destructive/30 bg-destructive/5'
          : isInflight
          ? 'border-primary/30 bg-primary/5'
          : 'border-border hover:bg-accent/50'
        }
      `}
    >
      <FileText className={`w-4 h-4 mt-0.5 flex-shrink-0 ${isInflight ? 'text-primary' : 'text-muted-foreground'}`} />
      <div className="flex-1 min-w-0">
        <p className="text-xs font-medium truncate">{doc.title}</p>
        <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center gap-1.5">
          <StatusIcon />
          <span>{statusLabel}</span>
          {doc.status === 'completed' && <span>· {doc.doc_type}</span>}
        </p>
        {isFailed && doc.ingestion_error && (
          <p className="text-[10px] text-destructive mt-1 line-clamp-2" title={doc.ingestion_error}>
            {doc.ingestion_error}
          </p>
        )}
      </div>
      <button
        className="opacity-0 group-hover:opacity-100 transition-opacity"
        onClick={onDelete}
        title="Eliminar"
      >
        <Trash2 className="w-3.5 h-3.5 text-muted-foreground hover:text-destructive" />
      </button>
    </div>
  );
}
