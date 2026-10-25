# Changelog

## 1.1.0

* Analog sticks now stop speech too: pushing a stick at least halfway in any direction counts as a press. Triggers need a half pull as well.
* Fixed speech caused by a controller press sometimes being cut off. If the game or NVDA starts speaking in response to the press before the add-on gets to it, that new speech is now kept and only the speech that was already playing is stopped.
* Fixed one press sometimes being detected twice, which could cut off the response a moment later.
* Each d-pad / POV hat direction on non-Xbox controllers now counts separately, so going from one direction straight to another stops speech.

## 1.0.1

* Set the last tested NVDA version to 2026.1 so the add-on can be submitted to the stable channel of the Add-on Store.

## 1.0.0

* Initial release.
