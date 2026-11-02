(vl-load-com)

;; GammaNederwiek MVP 0.4A.3 - read-only title-block discovery.
;; Fix: DXF group 330 already returns an entity name. Do not pass it to HANDENT.
;; 0.4A.3: GNI:DISCOVER-BATCH scans several staged drawings inside ONE Core
;; Console session. accoreconsole startup is ~4.7 s while the scan itself is
;; under a second, so batching is the only lever that removes that cost.

(defun GNI:SAFE (value / text)
  (if value
    (progn
      (setq text (vl-princ-to-string value))
      ;; 0.4C.28: translate EVERY tab/CR/LF. `vl-string-subst` replaces only the
      ;; first occurrence, so a value with two line breaks left one inside the
      ;; TSV field; the row then split and the merged values broke header
      ;; detection for that drawing (measured: DR-4110-0407-GA-0001).
      (setq text (vl-string-translate "\t\r\n" "   " text))
      text)
    ""))

(defun GNI:ROW (fileHandle values / first)
  (setq first T)
  (foreach value values
    (if first (setq first nil) (princ "\t" fileHandle))
    (princ (GNI:SAFE value) fileHandle))
  (princ "\n" fileHandle))

(defun GNI:POINT-TEXT (pointValue)
  (if (and pointValue (listp pointValue))
    (strcat
      (rtos (car pointValue) 2 6) ","
      (rtos (cadr pointValue) 2 6) ","
      (if (caddr pointValue) (rtos (caddr pointValue) 2 6) "0.000000"))
    ""))

(defun GNI:PARENT-DATA (entityData / owner ownerData)
  ;; The value of DXF 330 is already an ENAME in ENTGET output.
  (setq owner (cdr (assoc 330 entityData)))
  (if owner
    (setq ownerData (vl-catch-all-apply 'entget (list owner))))
  (if (vl-catch-all-error-p ownerData) nil ownerData))

(defun GNI:OWNER-HANDLE (entityData / owner ownerData)
  (setq owner (cdr (assoc 330 entityData)))
  (if owner
    (progn
      (setq ownerData (vl-catch-all-apply 'entget (list owner)))
      (if (vl-catch-all-error-p ownerData) "" (cdr (assoc 5 ownerData))))
    ""))

(defun GNI:DISCOVER-ENTITY (fileHandle entity / data entityType handle layer tag value point ownerData ownerType blockName ownerHandle)
  (setq data (vl-catch-all-apply 'entget (list entity)))
  (if (not (vl-catch-all-error-p data))
    (progn
      (setq entityType (cdr (assoc 0 data)))
      (setq handle (cdr (assoc 5 data)))
      (setq layer (cdr (assoc 8 data)))
      (setq tag (cdr (assoc 2 data)))
      (setq value (cdr (assoc 1 data)))
      (setq point (cdr (assoc 10 data)))
      (setq ownerHandle (GNI:OWNER-HANDLE data))
      (setq ownerData (GNI:PARENT-DATA data))
      (setq ownerType (if ownerData (cdr (assoc 0 ownerData)) ""))
      (setq blockName (if ownerData (cdr (assoc 2 ownerData)) ""))
      (GNI:ROW fileHandle
        (list (getvar "DWGNAME") "MODEL" entityType handle layer tag value
          (GNI:POINT-TEXT point) ownerHandle ownerType blockName)))))

(defun GNI:DISCOVER (outputPath / fileHandle selection index entity count)
  (princ "\nGNI_DISCOVERY_STAGE:OPEN_OUTPUT")
  (setq fileHandle (open outputPath "w"))
  (if fileHandle
    (progn
      (GNI:ROW fileHandle
        '("Drawing" "Space" "Entity Type" "Handle" "Layer" "Tag" "Value" "Insertion Point" "Owner Handle" "Owner Type" "Block Name"))
      (princ "\nGNI_DISCOVERY_STAGE:SELECT")
      (setq selection
        (ssget "_X"
          '((-4 . "<OR") (0 . "ATTRIB") (0 . "ATTDEF") (0 . "TEXT")
            (0 . "MTEXT") (-4 . "OR>") (410 . "Model"))))
      (setq count 0)
      (if selection
        (progn
          (setq index 0)
          (repeat (sslength selection)
            (setq entity (ssname selection index))
            (setq index (1+ index))
            (if entity
              (progn (GNI:DISCOVER-ENTITY fileHandle entity) (setq count (1+ count)))))))
      (close fileHandle)
      (princ (strcat "\nGNI_DISCOVERY_STAGE:WRITE_DONE:COUNT=" (itoa count)))
      (princ (strcat "\nGNI_TITLEBLOCK_DISCOVERY_OK:" outputPath)))
    (princ (strcat "\nGNI_TITLEBLOCK_DISCOVERY_ERROR:CANNOT_WRITE:" outputPath)))
  (princ))

;; Open one staged drawing, run GNI:DISCOVER on it, then close it again.
;; The drawing is handed back to the caller untouched: SAVEAS/QSAVE are never
;; called, so the staged copy stays byte-identical to the source.
;; 0.4A.4: use `(command-s)` rather than `(command)`. Measured on AutoCAD 2027
;; Core Console: inside a script, `(command "_.OPEN" path)` swallows the
;; following expression ("input.dwg_.OPEN" on one line, so the batch stopped
;; after the first drawing). `command-s` executes synchronously instead.
(defun GNI:DISCOVER-FILE (dwgPath outputPath / response)
  (princ (strcat "\nGNI_DISCOVERY_STAGE:OPEN:" dwgPath))
  (setq response
    (vl-catch-all-apply
      '(lambda ()
        (command-s "_.OPEN" dwgPath))))
  (if (vl-catch-all-error-p response)
    (progn
      (princ (strcat "\nGNI_DISCOVERY_FAILED:OPEN_FAILED:"
        (vl-catch-all-error-message response)))
      nil)
    (progn
      (GNI:DISCOVER outputPath)
      (setq response (vl-catch-all-apply '(lambda () (command-s "_.CLOSE"))))
      T)))

(defun GNI:DISCOVER-BATCH (jobFile / jobStream line dwgPath outputPath ok done)
  (princ "\nGNI_DISCOVERY_STAGE:BATCH_START")
  (setq jobStream (open jobFile "r"))
  (if (not jobStream)
    (princ (strcat "\nGNI_TITLEBLOCK_DISCOVERY_ERROR:CANNOT_READ_JOB:" jobFile))
    (progn
      (setq done 0)
      (while (setq line (read-line jobStream))
        (setq dwgPath line)
        (setq outputPath (read-line jobStream))
        (if (and dwgPath outputPath (/= outputPath ""))
          (progn
            (setq ok (GNI:DISCOVER-FILE dwgPath outputPath))
            (if ok (setq done (1+ done))))))
      (close jobStream)
      (princ (strcat "\nGNI_DISCOVERY_BATCH_DONE:DRAWINGS=" (itoa done)))))
  (princ))

(princ "\nGNI_TITLEBLOCK_DISCOVERY_0_4A_LOADED")
(princ "\nGNI_TITLEBLOCK_DISCOVERY_0_4A_1_OWNER_FIX")
(princ "\nGNI_TITLEBLOCK_DISCOVERY_0_4A_3_BATCH")
(princ "\nGNI_TITLEBLOCK_DISCOVERY_0_4A_4_COMMAND_S")
(princ)
