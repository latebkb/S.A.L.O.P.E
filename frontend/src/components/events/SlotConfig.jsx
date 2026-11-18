import { useEffect, useState } from 'react'
import { eventsApi } from '../../api/events.js'
import { IconTrash } from '@tabler/icons-react'
import '../../styles/planning.css'

const customKey = (nom, slotId) => `${nom}|${slotId}`
const customState = (shiftsCustom) =>
  Object.fromEntries(shiftsCustom.map((sc) => [customKey(sc.nom, sc.creneau_id), sc.max_personnes]))

export default function SlotConfig({ slots, shiftsCustom = [], eventId, onSaved, disabled }) {
  const [maxPersonnes, setMaxPersonnes] = useState(
    Object.fromEntries(slots.map((s) => [s.id, s.max_personnes]))
  )
  // Les créneaux peuvent changer pendant que l'onglet est monté (suppression
  // ci-dessous) : resynchroniser sans perdre les éditions en cours.
  useEffect(() => {
    setMaxPersonnes((prev) =>
      Object.fromEntries(slots.map((s) => [s.id, prev[s.id] ?? s.max_personnes]))
    )
  }, [slots])
  // Shifts custom (entrée…) : une colonne par nom, case vide = pas de shift
  // sur ce créneau, nombre = max personnes du shift.
  const [customMax, setCustomMax] = useState(() => customState(shiftsCustom))
  const [extraNames, setExtraNames] = useState([])
  useEffect(() => {
    setCustomMax(customState(shiftsCustom))
    setExtraNames([])
  }, [shiftsCustom])
  const customNames = [...new Set([...shiftsCustom.map((sc) => sc.nom), ...extraNames])]
  const [newName, setNewName] = useState('')
  const addCustomName = () => {
    const nom = newName.trim()
    if (!nom || customNames.includes(nom)) return
    setExtraNames((prev) => [...prev, nom])
    // Pré-remplit 1 personne sur chaque créneau, à ajuster/vider
    setCustomMax((prev) => ({
      ...prev,
      ...Object.fromEntries(slots.map((sl) => [customKey(nom, sl.id), 1])),
    }))
    setNewName('')
  }

  const [saving, setSaving] = useState(false)
  const [applyAll, setApplyAll] = useState(3)

  const saveCustoms = () => {
    const existing = Object.fromEntries(
      shiftsCustom.map((sc) => [customKey(sc.nom, sc.creneau_id), sc])
    )
    const calls = []
    for (const nom of customNames) {
      for (const sl of slots) {
        const k = customKey(nom, sl.id)
        const v = parseInt(customMax[k], 10)
        const row = existing[k]
        if (row && !(v >= 1)) calls.push(eventsApi.deleteShiftCustom(eventId, row.id))
        else if (row && v !== row.max_personnes)
          calls.push(eventsApi.updateShiftCustom(eventId, row.id, { max_personnes: v }))
        else if (!row && v >= 1)
          calls.push(eventsApi.createShiftsCustom(eventId, { nom, creneau_ids: [sl.id], max_personnes: v }))
      }
    }
    return calls
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await Promise.all([
        ...slots.map((s) =>
          eventsApi.updateSlot(eventId, s.id, { max_personnes: maxPersonnes[s.id] })
        ),
        ...saveCustoms(),
      ])
      onSaved()
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (slot) => {
    if (!confirm('Supprimer ce créneau ? Les réponses et affectations associées seront perdues.')) return
    await eventsApi.deleteSlot(eventId, slot.id)
    onSaved()
  }

  const fmt = (dt) =>
    new Date(dt).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', hour12: false })
  const fmtDate = (dt) =>
    new Date(dt).toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' })

  if (slots.length === 0) {
    return (
      <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--text-muted)', fontSize: 12 }}>
        Aucun créneau. Ils se définissent à la création de l'événement, ou via un import CSV.
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <p style={{ fontSize: 15, color: 'var(--text-secondary)' }}>
        Définissez le nombre maximum de personnes par créneau. Ajoutez des shifts
        custom (ex. entrée) : case vide = pas de shift à cette heure. Les dispos du
        créneau valent pour le bar comme pour les shifts custom.
      </p>

      {!disabled && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: 'var(--text-muted)', flexWrap: 'wrap' }}>
          Shift custom :
          <input
            value={newName}
            maxLength={40}
            placeholder="Entrée"
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addCustomName()}
            style={{
              width: 140,
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border-mid)',
              borderRadius: 'var(--radius-sm)',
              padding: '5px 8px',
              fontSize: 14,
              color: 'var(--text-primary)',
              outline: 'none',
            }}
          />
          <button
            onClick={addCustomName}
            disabled={!newName.trim()}
            style={{
              background: 'var(--bg-high)',
              color: 'var(--text-secondary)',
              border: '1px solid var(--border)',
              borderRadius: 'var(--radius-sm)',
              padding: '5px 12px',
              fontSize: 13,
              cursor: 'pointer',
              fontFamily: "'DM Sans', sans-serif",
            }}
          >
            Ajouter
          </button>
        </div>
      )}

      {!disabled && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: 'var(--text-muted)' }}>
          Appliquer à tous :
          <input
            type="number"
            min="1"
            max="20"
            value={applyAll}
            onChange={(e) => setApplyAll(e.target.value)}
            style={{
              width: 60,
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border-mid)',
              borderRadius: 'var(--radius-sm)',
              padding: '5px 8px',
              textAlign: 'center',
              fontSize: 14,
              color: 'var(--text-primary)',
              outline: 'none',
              fontFamily: 'DM Mono, monospace',
            }}
          />
          <button
            onClick={() => {
              const v = parseInt(applyAll, 10)
              if (!Number.isNaN(v) && v >= 1) {
                setMaxPersonnes(Object.fromEntries(slots.map((s) => [s.id, v])))
              }
            }}
            style={{
              background: 'var(--bg-high)',
              color: 'var(--text-secondary)',
              border: '1px solid var(--border)',
              borderRadius: 'var(--radius-sm)',
              padding: '5px 12px',
              fontSize: 13,
              cursor: 'pointer',
              fontFamily: "'DM Sans', sans-serif",
            }}
          >
            Appliquer
          </button>
        </div>
      )}

      <div className="planning-wrapper">
        <table className="planning-table">
          <thead>
            <tr>
              <th>Créneau</th>
              <th style={{ width: 160 }}>Max bar</th>
              {customNames.map((nom) => (
                <th key={nom} style={{ width: 120 }}>Max {nom}</th>
              ))}
              {!disabled && <th style={{ width: 40 }} />}
            </tr>
          </thead>
          <tbody>
            {slots.map((s) => (
              <tr key={s.id}>
                <td>
                  {fmtDate(s.heure_debut)} · {fmt(s.heure_debut)} – {fmt(s.heure_fin)}
                </td>
                <td>
                  <input
                    type="number"
                    min="1"
                    max="20"
                    value={maxPersonnes[s.id] ?? 3}
                    disabled={disabled}
                    onChange={(e) =>
                      setMaxPersonnes((prev) => ({
                        ...prev,
                        [s.id]: parseInt(e.target.value, 10),
                      }))
                    }
                    style={{
                      width: 70,
                      background: 'var(--bg-elevated)',
                      border: '1px solid var(--border-mid)',
                      borderRadius: 'var(--radius-sm)',
                      padding: '6px 8px',
                      textAlign: 'center',
                      fontSize: 15,
                      color: 'var(--text-primary)',
                      outline: 'none',
                      fontFamily: 'DM Mono, monospace',
                    }}
                  />
                </td>
                {customNames.map((nom) => (
                  <td key={nom}>
                    <input
                      type="number"
                      min="0"
                      max="20"
                      placeholder="—"
                      value={customMax[customKey(nom, s.id)] ?? ''}
                      disabled={disabled}
                      onChange={(e) =>
                        setCustomMax((prev) => ({ ...prev, [customKey(nom, s.id)]: e.target.value }))
                      }
                      style={{
                        width: 60,
                        background: 'var(--bg-elevated)',
                        border: '1px solid var(--border-mid)',
                        borderRadius: 'var(--radius-sm)',
                        padding: '6px 8px',
                        textAlign: 'center',
                        fontSize: 15,
                        color: 'var(--text-primary)',
                        outline: 'none',
                        fontFamily: 'DM Mono, monospace',
                      }}
                    />
                  </td>
                ))}
                {!disabled && (
                  <td>
                    <button
                      onClick={() => handleDelete(s)}
                      title="Supprimer ce créneau"
                      style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--red)', display: 'flex', alignItems: 'center', padding: 4 }}
                    >
                      <IconTrash size={14} stroke={1.5} />
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!disabled && (
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button
            onClick={handleSave}
            disabled={saving}
            style={{
              background: 'var(--accent)',
              color: '#fff',
              border: 'none',
              borderRadius: 'var(--radius-sm)',
              padding: '9px 20px',
              fontSize: 14,
              fontWeight: 500,
              cursor: saving ? 'not-allowed' : 'pointer',
              opacity: saving ? 0.6 : 1,
              fontFamily: "'DM Sans', sans-serif",
            }}
          >
            {saving ? 'Enregistrement…' : 'Enregistrer'}
          </button>
        </div>
      )}
    </div>
  )
}
