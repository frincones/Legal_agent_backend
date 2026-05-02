import { ExternalLink, Trash2, GitBranch } from 'lucide-react';
import VigenciaIndicator from './VigenciaIndicator';
import type { NormaItem } from '../../lib/api';

interface NormaCardProps {
  norma: NormaItem;
  onDelete?: (id: string) => void;
  onViewChain?: (id: string) => void;
}

export default function NormaCard({ norma, onDelete, onViewChain }: NormaCardProps) {
  const nombre = `${norma.tipo} ${norma.numero ?? ''} de ${norma.anio ?? ''}`;

  return (
    <div className="flex items-start gap-3 p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800/50 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">
            {norma.titulo || nombre}
          </h4>
          <VigenciaIndicator estado={norma.estado} compact />
        </div>

        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{nombre}</p>

        {norma.sector && (
          <span className="inline-block mt-1 px-1.5 py-0.5 text-[10px] font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 rounded">
            {norma.sector}
          </span>
        )}

        {norma.temas && norma.temas.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1">
            {norma.temas.slice(0, 3).map((t, i) => (
              <span key={i} className="px-1.5 py-0.5 text-[10px] bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 rounded">
                {t}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="flex items-center gap-1 flex-shrink-0">
        {norma.fuente_url && (
          <a
            href={norma.fuente_url}
            target="_blank"
            rel="noopener noreferrer"
            className="p-1.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-400 hover:text-blue-500 transition-colors"
            title="Ver en fuente oficial"
          >
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        )}
        {onViewChain && (
          <button
            onClick={() => onViewChain(norma.id)}
            className="p-1.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-400 hover:text-purple-500 transition-colors"
            title="Ver cadena de derogaciones"
          >
            <GitBranch className="w-3.5 h-3.5" />
          </button>
        )}
        {onDelete && (
          <button
            onClick={() => onDelete(norma.id)}
            className="p-1.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-400 hover:text-red-500 transition-colors"
            title="Eliminar norma"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
