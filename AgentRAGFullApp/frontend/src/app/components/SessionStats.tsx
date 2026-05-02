interface SessionStatsProps {
  caseState: Record<string, unknown> | null;
}

export default function SessionStats({ caseState }: SessionStatsProps) {
  if (!caseState || !caseState.turn_count) return null;

  const turns = (caseState.turn_count as number) || 0;
  const norms = ((caseState.norms_cited as string[]) || []).length;
  const juris = ((caseState.jurisprudence_cited as string[]) || []).length;
  const areas = ((caseState.areas_involved as string[]) || []).join(', ');
  const caseIndex = (caseState.case_index as number) || 1;
  const archivedCount = ((caseState.archived_cases as unknown[]) || []).length;
  const hasSummary = Boolean(caseState.summary);

  return (
    <div className="flex items-center gap-2 text-[10px] text-muted-foreground/70 px-1">
      {(caseIndex > 1 || archivedCount > 0) && (
        <>
          <span
            className="px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 font-medium"
            title={`Caso ${caseIndex} de la sesión. ${archivedCount} caso${archivedCount === 1 ? '' : 's'} archivado${archivedCount === 1 ? '' : 's'}.`}
          >
            Caso {caseIndex}
          </span>
          <span className="opacity-30">|</span>
        </>
      )}
      <span>Turno {turns}</span>
      {norms > 0 && <><span className="opacity-30">|</span><span>{norms} normas</span></>}
      {juris > 0 && <><span className="opacity-30">|</span><span>{juris} sentencias</span></>}
      {areas && <><span className="opacity-30">|</span><span className="truncate max-w-[150px]">{areas}</span></>}
      {hasSummary && (
        <>
          <span className="opacity-30">|</span>
          <span
            className="text-blue-600 dark:text-blue-400"
            title="El historial fue comprimido automáticamente para mantener el contexto manejable."
          >
            comprimido
          </span>
        </>
      )}
    </div>
  );
}
