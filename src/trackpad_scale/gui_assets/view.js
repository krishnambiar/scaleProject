'use strict';

// Everyday weighing is independent of the experimental calibration workflow.
function scaleView(state, connected) {
  const reading = state.live_reading;
  const current = connected && state.running && Number.isFinite(reading?.estimated_grams);
  const saved = state.last_reading?.estimated_grams;
  const hasSaved = Number.isFinite(saved);
  const view = (value, label, instruction, canZero = false) => ({value, label, instruction, canZero});
  if (!connected) {
    return view(hasSaved ? saved : null, hasSaved ? 'Last estimate · disconnected' : 'Disconnected', 'Connection lost. Reopen the local app to continue.');
  }
  if (state.error) {
    return view(null, 'Unavailable', 'Could not connect. Press Start to try again.');
  }
  if (!state.running) {
    return view(hasSaved ? saved : null, hasSaved ? 'Last estimate · stopped' : 'Ready', 'Press Start, then rest one finger lightly on the trackpad.');
  }
  if (state.status === 'connecting') {
    return view(null, 'Connecting', 'Connecting to your trackpad…');
  }
  if (state.reading_issue === 'invalid_pressure') {
    return view(null, 'Pressure unavailable', 'The trackpad did not provide a valid pressure reading. Keep your finger lightly in contact while readings resume.');
  }
  if (current) {
    return view(reading.estimated_grams, reading.zeroed ? 'Live estimate · zeroed' : 'Live estimate', reading.zeroed
      ? 'Place your object on the trackpad or paper. Keep your finger lightly in contact while weighing.'
      : 'Keep one finger lightly in contact. Lay down dry paper first if needed, then press Zero or Space before placing your object.', true);
  }
  return view(hasSaved ? saved : null, hasSaved ? 'Last estimate · not updating' : 'Ready for contact', hasSaved
    ? 'Contact ended; the last estimate is shown. Rest your finger lightly and zero again for a new object.'
    : 'Rest one finger lightly on the trackpad and keep it there while weighing. Readings appear immediately.');
}

if (typeof module !== 'undefined') module.exports = {scaleView};
