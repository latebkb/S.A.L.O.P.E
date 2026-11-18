import { useState, useEffect, useRef, useMemo } from 'react'
import { IconPlus, IconUserPlus, IconArrowBackUp, IconTrash } from '@tabler/icons-react'
import { eventsApi } from '../../api/events.js'
import PlanningLegend from './PlanningLegend.jsx'
import '../../styles/planning.css'
import { popupPos } from '../../utils/popupPos.js'

export default function PlanningGrid({
  event,
  slots,
  members,
  assignments,
  shiftsCustom = [],
  affinities,
  availabilities = [],
  excludedMemberIds = new Set(),
  onAssignmentChange,
  onUndoableAction,
  canUndo = false,
  onUndo,
  canClear = false,
  onClear,
  eventId,
  disabled,
  highlight,
}) {
  const [editCell, setEditCell] = useState(null) // { memberId, slotId, x, y }
  const [cellLoading, setCellLoading] = useState(null)
  const [cellError, setCellError] = useState(null)
  const [extraMemberIds, setExtraMemberIds] = useState(new Set())
  const [addOpen, setAddOpen] = useState(false)
  const [dragging, setDragging] = useState(null) // { asgnId, memberId, fromSlotId, type }
  const popupRef = useRef(null)
  const addRef = useRef(null)

  // Close popups on outside click
  useEffect(() => {
    if (!editCell && !addOpen) return
    const onDown = (e) => {
      if (popupRef.current && !popupRef.current.contains(e.target)) setEditCell(null)
      if (addRef.current && !addRef.current.contains(e.target)) setAddOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [editCell, addOpen])

  // Auto-dismiss error after 3.5s
  useEffect(() => {
    if (!cellError) return
    const t = setTimeout(() => setCellError(null), 3500)
    return () => clearTimeout(t)
  }, [cellError])

  // Availability per cell: "yes" | "no" | "maybe"
  const availMap = useMemo(() => {
    const m = {}
    for (const a of availabilities) m[`${a.membre_id}-${a.creneau_id}`] = a.statut
    return m
  }, [availabilities])

  const asgMap = {}
  for (const a of assignments) {
    asgMap[`${a.membre_id}-${a.creneau_id}`] = a
  }

  const shiftsById = useMemo(
    () => Object.fromEntries(shiftsCustom.map((sc) => [sc.id, sc])),
    [shiftsCustom]
  )
  const shiftsAtSlot = (slotId) => shiftsCustom.filter((sc) => sc.creneau_id === slotId)
  const customNames = [...new Set(shiftsCustom.map((sc) => sc.nom))]

  // Membres avec au moins un créneau dispo (yes/maybe)
  const availableIds = useMemo(
    () =>
      new Set(
        availabilities
          .filter((a) => a.statut === 'yes' || a.statut === 'maybe')
          .map((a) => a.membre_id)
      ),
    [availabilities]
  )

  const respondentIds = useMemo(
    () => new Set(availabilities.map((a) => a.membre_id)),
    [availabilities]
  )

  // Tous les répondant·e·s non exclus (onglet Participants) sont affichés, y
  // compris ceux qui ont répondu « pas dispo » partout : le bureau peut leur
  // ajouter des heures s'ils se rendent dispo après coup. Les non-répondants
  // restent ajoutables via « Ajouter un membre ». Dispos d'abord, puis alpha.
  const assignedMemberIds = new Set(assignments.map((a) => a.membre_id))
  const visibleMembers = members
    .filter((m) =>
      assignedMemberIds.has(m.id) || extraMemberIds.has(m.id) ||
      (respondentIds.has(m.id) && !excludedMemberIds.has(m.id))
    )
    .sort((a, b) =>
      (availableIds.has(b.id) - availableIds.has(a.id)) || a.nom.localeCompare(b.nom)
    )
  const visibleIds = new Set(visibleMembers.map((m) => m.id))
  const addableMembers = members
    .filter((m) => !visibleIds.has(m.id))
    .sort((a, b) =>
      (respondentIds.has(b.id) - respondentIds.has(a.id)) ||
      a.nom.localeCompare(b.nom)
    )

  const slotTotals = {}
  const customTotals = {}
  for (const a of assignments) {
    if (a.shift_custom_id == null) slotTotals[a.creneau_id] = (slotTotals[a.creneau_id] || 0) + 1
    else customTotals[a.shift_custom_id] = (customTotals[a.shift_custom_id] || 0) + 1
  }

  // Heures par membre — convention du système : 1 créneau = 1 heure
  const memberHours = {}
  for (const a of assignments) {
    memberHours[a.membre_id] = (memberHours[a.membre_id] || 0) + 1
  }
  const fmtHours = (h) => (h ? `${h} h` : '—')

  const fmtTime = (dt) =>
    new Date(dt).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', hour12: false })

  const getCellClass = (asgn, memberId, slotId) => {
    const classes = []
    if (asgn) {
      classes.push(
        asgn.shift_custom_id != null ? 'cell-custom' : asgn.type === 'respo_bar' ? 'cell-respo' : 'cell-staff'
      )
      const dispo = availMap[`${memberId}-${slotId}`]
      if (dispo !== 'yes' && dispo !== 'maybe') classes.push('cell-forced')
      if (dragging?.asgnId === asgn.id) classes.push('cell-drag-source')
    } else {
      const dispo = availMap[`${memberId}-${slotId}`]
      if (dispo === 'yes') classes.push('cell-dispo-yes')
      else if (dispo === 'maybe') classes.push('cell-dispo-maybe')
      else if (dispo === 'no') classes.push('cell-dispo-no')
      if (dragging) classes.push('cell-drop-ok')
    }
    if (
      highlight &&
      ((highlight.slotId === slotId && (highlight.memberId == null || highlight.memberId === memberId)) ||
        (highlight.slotId == null && highlight.memberId === memberId))
    ) {
      classes.push('cell-highlight')
    }
    return classes.join(' ')
  }

  const getCellLabel = (asgn) => {
    if (!asgn) return ''
    if (asgn.shift_custom_id != null) return shiftsById[asgn.shift_custom_id]?.nom ?? 'Autre'
    if (asgn.type === 'respo_bar') return 'Respo'
    return 'Staff'
  }

  const handleCellClick = (e, memberId, slotId) => {
    if (disabled) return
    setCellError(null)
    if (editCell?.memberId === memberId && editCell?.slotId === slotId) {
      setEditCell(null)
      return
    }
    const rect = e.currentTarget.getBoundingClientRect()
    setEditCell({ memberId, slotId, ...popupPos(rect, 140, 170 + 34 * shiftsAtSlot(slotId).length) })
  }

  // shiftId : null = bar, sinon id du shift custom sur ce créneau
  const handleAssign = async (memberId, slotId, type, shiftId = null) => {
    const key = `${memberId}-${slotId}`
    setCellLoading(key)
    setCellError(null)
    try {
      const existing = asgMap[key]
      if (type === 'none') {
        if (existing) {
          await eventsApi.deleteAssignment(eventId, existing.id)
          onUndoableAction?.([{
            op: 'add', creneau_id: slotId, membre_id: memberId, type: existing.type,
            shift_custom_id: existing.shift_custom_id,
          }])
        }
      } else if (existing) {
        await eventsApi.updateAssignment(eventId, existing.id, { type, shift_custom_id: shiftId })
        if (existing.type !== type || (existing.shift_custom_id ?? null) !== shiftId) {
          onUndoableAction?.([{
            op: 'update', creneau_id: slotId, membre_id: memberId, type: existing.type,
            shift_custom_id: existing.shift_custom_id,
          }])
        }
      } else {
        await eventsApi.addAssignment(eventId, { creneau_id: slotId, membre_id: memberId, type, shift_custom_id: shiftId })
        onUndoableAction?.([{ op: 'delete', creneau_id: slotId, membre_id: memberId }])
      }
      const fresh = await eventsApi.listAssignments(eventId)
      onAssignmentChange(fresh)
    } catch (err) {
      setCellError({ memberId, slotId, x: editCell?.x ?? 0, y: editCell?.y ?? 0, message: err.response?.data?.detail || "Erreur lors de l'affectation" })
    } finally {
      setCellLoading(null)
      setEditCell(null)
    }
  }

  const handleDrop = async (toMemberId, toSlotId) => {
    const d = dragging
    setDragging(null)
    if (!d) return
    if (d.memberId === toMemberId && d.fromSlotId === toSlotId) return
    const key = `${toMemberId}-${toSlotId}`
    setCellLoading(key)
    setCellError(null)
    // Shift custom : suit le shift de même nom sur le créneau cible, sinon bar
    const fromShift = d.shiftId != null ? shiftsById[d.shiftId] : null
    const toShiftId = fromShift
      ? (shiftsAtSlot(toSlotId).find((sc) => sc.nom === fromShift.nom)?.id ?? null)
      : null
    try {
      await eventsApi.deleteAssignment(eventId, d.asgnId)
      try {
        await eventsApi.addAssignment(eventId, {
          creneau_id: toSlotId, membre_id: toMemberId, type: d.type, shift_custom_id: toShiftId,
        })
        onUndoableAction?.([
          { op: 'delete', creneau_id: toSlotId, membre_id: toMemberId },
          { op: 'add', creneau_id: d.fromSlotId, membre_id: d.memberId, type: d.type, shift_custom_id: d.shiftId },
        ])
      } catch (err) {
        // rollback best-effort : remettre l'affectation d'origine
        await eventsApi
          .addAssignment(eventId, {
            creneau_id: d.fromSlotId, membre_id: d.memberId, type: d.type, shift_custom_id: d.shiftId,
          })
          .catch(() => {})
        throw err
      }
    } catch {
      // l'intercepteur axios affiche déjà l'erreur en toast
    } finally {
      const fresh = await eventsApi.listAssignments(eventId).catch(() => null)
      if (fresh) onAssignmentChange(fresh)
      setCellLoading(null)
    }
  }

  const addMemberRow = (memberId) => {
    setExtraMemberIds((prev) => new Set(prev).add(memberId))
    setAddOpen(false)
  }

  const addButton = !disabled && addableMembers.length > 0 && (
    <div style={{ position: 'relative' }} ref={addRef}>
      <button
        onClick={() => setAddOpen((o) => !o)}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
          border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
          padding: '6px 12px', fontSize: 13, cursor: 'pointer',
          fontFamily: "'DM Sans', sans-serif",
        }}
      >
        <IconUserPlus size={13} stroke={1.5} />
        Ajouter un membre
      </button>
      {addOpen && (
        <div style={{
          position: 'absolute', zIndex: 100, top: '100%', left: 0, marginTop: 6,
          background: 'var(--bg-elevated)', border: '1px solid var(--border-mid)',
          borderRadius: 'var(--radius-md)', padding: 6, minWidth: 220, maxHeight: 260,
          overflowY: 'auto', boxShadow: '0 4px 16px rgba(0,0,0,0.35)',
        }}>
          {addableMembers.map((m) => (
            <button
              key={m.id}
              onClick={() => addMemberRow(m.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                textAlign: 'left', padding: '7px 10px', fontSize: 14,
                background: 'transparent', color: 'var(--text-primary)',
                border: 'none', borderRadius: 4, cursor: 'pointer',
                fontFamily: "'DM Sans', sans-serif",
              }}
              className="hover-high"
            >
              <IconPlus size={12} stroke={1.5} color="var(--text-muted)" />
              {m.prenom} {m.nom}
              {!respondentIds.has(m.id) && (
                <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-muted)' }}>sans réponse</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )

  if (visibleMembers.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14, padding: '40px 0' }}>
        <p style={{ color: 'var(--text-muted)', fontSize: 15 }}>
          Aucune affectation. Lancez la génération automatique, ou placez des membres à la main.
        </p>
        {addButton}
      </div>
    )
  }

  return (
    <div>
      <div className="planning-wrapper">
        <div style={{ overflowX: 'auto' }}>
          <table className="planning-table">
            <thead>
              <tr>
                <th>Membre</th>
                {slots.map((s) => (
                  <th key={s.id} className={highlight?.slotId === s.id ? 'th-highlight' : ''}>
                    <div style={{ fontFamily: 'DM Mono, monospace', fontSize: 15, fontWeight: 500 }}>
                      {fmtTime(s.heure_debut)}–{fmtTime(s.heure_fin)}
                    </div>
                  </th>
                ))}
                <th className="col-hours">Heures</th>
              </tr>
            </thead>
            <tbody>
              {visibleMembers.map((member) => (
                <tr key={member.id}>
                  <td>
                    {member.prenom} {member.nom}
                    {member.roles?.includes('bureau') && (
                      <span className="bureau-dot" title="Bureau" />
                    )}
                  </td>
                  {slots.map((slot) => {
                    const asgn = asgMap[`${member.id}-${slot.id}`]
                    const cellKey = `${member.id}-${slot.id}`
                    const isLoading = cellLoading === cellKey
                    const dropOk = !disabled && dragging && !asgn
                    return (
                      <td
                        key={slot.id}
                        className={getCellClass(asgn, member.id, slot.id)}
                        onClick={(e) => handleCellClick(e, member.id, slot.id)}
                        draggable={!disabled && !!asgn}
                        onDragStart={
                          !disabled && asgn
                            ? (e) => {
                                e.dataTransfer.effectAllowed = 'move'
                                setDragging({ asgnId: asgn.id, memberId: member.id, fromSlotId: slot.id, type: asgn.type, shiftId: asgn.shift_custom_id ?? null })
                              }
                            : undefined
                        }
                        onDragEnd={() => setDragging(null)}
                        onDragOver={(e) => { if (dropOk) e.preventDefault() }}
                        onDrop={dropOk ? (e) => { e.preventDefault(); handleDrop(member.id, slot.id) } : undefined}
                        style={{ cursor: !disabled ? 'pointer' : 'default', opacity: isLoading ? 0.5 : 1, position: 'relative' }}
                      >
                        {asgn && <span>{getCellLabel(asgn)}</span>}
                        {asgn?.source === 'manuel' && (
                          <span className="manual-dot" title="Affectation manuelle (conservée à la régénération)" />
                        )}
                      </td>
                    )
                  })}
                  <td className="col-hours">{fmtHours(memberHours[member.id])}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td>Staffés / max</td>
                {slots.map((s) => {
                  const n = slotTotals[s.id] || 0
                  const max = s.max_personnes
                  const cls = n < max ? 'foot-under' : n > max ? 'foot-over' : 'foot-full'
                  return (
                    <td key={s.id} className={cls} title={n < max ? 'Créneau sous-staffé' : n > max ? 'Au-delà du max' : 'Complet'}>
                      {n}/{max}
                    </td>
                  )
                })}
                <td className="col-hours" />
              </tr>
              {customNames.map((nom) => (
                <tr key={nom}>
                  <td>{nom} / max</td>
                  {slots.map((s) => {
                    const sc = shiftsAtSlot(s.id).find((x) => x.nom === nom)
                    if (!sc) return <td key={s.id} />
                    const n = customTotals[sc.id] || 0
                    const cls = n < sc.max_personnes ? 'foot-under' : n > sc.max_personnes ? 'foot-over' : 'foot-full'
                    return <td key={s.id} className={cls}>{n}/{sc.max_personnes}</td>
                  })}
                  <td className="col-hours" />
                </tr>
              ))}
            </tfoot>
          </table>
        </div>
      </div>

      {/* Cell error tooltip */}
      {cellError && (
        <div style={{
          position: 'fixed', zIndex: 40, left: cellError.x, top: cellError.y,
          background: 'var(--red-soft)', border: '1px solid rgba(240, 82, 82,0.3)',
          borderRadius: 'var(--radius-sm)', padding: '5px 10px', fontSize: 13,
          color: 'var(--red)', whiteSpace: 'nowrap',
        }}>
          {cellError.message}
        </div>
      )}

      {/* Fixed popup for cell editing */}
      {editCell && !disabled && (() => {
        const asgn = asgMap[`${editCell.memberId}-${editCell.slotId}`]
        return (
          <div
            ref={popupRef}
            onMouseDown={(e) => e.stopPropagation()}
            style={{
              position: 'fixed',
              zIndex: 9999,
              left: editCell.x,
              top: editCell.y,
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border-mid)',
              borderRadius: 'var(--radius-md)',
              padding: 6,
              minWidth: 130,
              boxShadow: '0 4px 16px rgba(0,0,0,0.35)',
            }}
          >
            <button onClick={() => handleAssign(editCell.memberId, editCell.slotId, 'staff')} style={editorBtn('var(--green-soft)', 'var(--green)')}>
              Staff
            </button>
            {shiftsAtSlot(editCell.slotId).map((sc) => (
              <button
                key={sc.id}
                onClick={() => handleAssign(editCell.memberId, editCell.slotId, 'staff', sc.id)}
                style={editorBtn('var(--accent-soft)', 'var(--accent)')}
              >
                {sc.nom}
              </button>
            ))}
            {event.type === 'soiree' && (
              <button onClick={() => handleAssign(editCell.memberId, editCell.slotId, 'respo_bar')} style={editorBtn('var(--red-soft)', 'var(--red)')}>
                Respo bar
              </button>
            )}
            {asgn && (
              <button onClick={() => handleAssign(editCell.memberId, editCell.slotId, 'none')} style={editorBtn('transparent', 'var(--text-muted)')}>
                Retirer
              </button>
            )}
            <button
              onClick={() => setEditCell(null)}
              style={{ ...editorBtn('transparent', 'var(--text-muted)'), borderTop: '1px solid var(--border)', marginTop: 2, paddingTop: 6 }}
            >
              Annuler
            </button>
          </div>
        )
      })()}

      {/* Legend + actions */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '10px 14px 4px', flexWrap: 'wrap' }}>
        {addButton}
        {!disabled && (
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              onClick={onUndo}
              disabled={!canUndo}
              title="Annuler la dernière modification (Ctrl+Z)"
              style={{
                ...footerBtnStyle,
                color: 'var(--text-secondary)',
                border: '1px solid var(--border)',
                opacity: canUndo ? 1 : 0.4,
                cursor: canUndo ? 'pointer' : 'not-allowed',
              }}
            >
              <IconArrowBackUp size={13} stroke={1.5} />
              Annuler (Ctrl+Z)
            </button>
            {onClear && (
              <button
                onClick={onClear}
                disabled={!canClear}
                title="Supprimer toutes les affectations pour repartir d'un planning vide"
                style={{
                  ...footerBtnStyle,
                  color: canClear ? 'var(--red)' : 'var(--text-secondary)',
                  border: `1px solid ${canClear ? 'rgba(240, 82, 82, 0.3)' : 'var(--border)'}`,
                  opacity: canClear ? 1 : 0.4,
                  cursor: canClear ? 'pointer' : 'not-allowed',
                }}
              >
                <IconTrash size={13} stroke={1.5} />
                Vider le planning
              </button>
            )}
          </div>
        )}
        {!disabled && (
          <span style={{ fontSize: 14, color: 'var(--accent)', marginLeft: 'auto' }}>
            Cliquez sur une cellule pour modifier, ou glissez-déposez une affectation
          </span>
        )}
      </div>
      <PlanningLegend />
    </div>
  )
}

const editorBtn = (bg, color) => ({
  display: 'block',
  width: '100%',
  textAlign: 'left',
  padding: '7px 12px',
  fontSize: 14,
  background: bg,
  color,
  border: 'none',
  borderRadius: 4,
  cursor: 'pointer',
  fontFamily: "'DM Sans', sans-serif",
  transition: 'background 120ms ease',
})

const footerBtnStyle = {
  display: 'inline-flex', alignItems: 'center', gap: 6,
  background: 'var(--bg-elevated)',
  borderRadius: 'var(--radius-sm)',
  padding: '6px 12px', fontSize: 13,
  fontFamily: "'DM Sans', sans-serif",
}
