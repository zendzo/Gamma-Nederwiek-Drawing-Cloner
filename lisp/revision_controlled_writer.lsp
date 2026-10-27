(vl-load-com)

;; MVP 0.4B.2.3: balanced, shutdown-safe controlled writer.

(defun GNI:CLEAN (v / s)
  (setq s (if v (vl-princ-to-string v) ""))
  (setq s (vl-string-subst " " "\t" s))
  (setq s (vl-string-subst " " "\r" s))
  (setq s (vl-string-subst " " "\n" s))
  s)

(defun GNI:ROW (f values / first)
  (setq first T)
  (foreach v values
    (if first (setq first nil) (princ "\t" f))
    (princ (GNI:CLEAN v) f))
  (princ "\n" f))

(defun GNI:ENSURE-LAYER (name)
  (if (tblsearch "LAYER" name)
    (progn
      (if GNI:TARGET-LAYER-COLOR
        (command-s "_.-LAYER" "_Color" (itoa GNI:TARGET-LAYER-COLOR) name ""))
      T)
    (if (entmake (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord")
      '(100 . "AcDbLayerTableRecord") (cons 2 name) '(70 . 0)
      (cons 62 (if GNI:TARGET-LAYER-COLOR GNI:TARGET-LAYER-COLOR 6)) '(6 . "Continuous"))) T nil)))

(defun GNI:SET-VALUE (e d value / p r)
  (setq p (assoc 1 d))
  (if p
    (progn
      (setq r (vl-catch-all-apply 'entmod
        (list (subst (cons 1 value) p d))))
      (if (or (vl-catch-all-error-p r) (null r)) nil
        (progn (vl-catch-all-apply 'entupd (list e)) T)))
    nil))

(defun GNI:SET-LAYER (e d name / p r)
  (if (and (/= name "") (GNI:ENSURE-LAYER name))
    (progn
      (setq p (assoc 8 d))
      (setq r (vl-catch-all-apply 'entmod
        (list (subst (cons 8 name) p d))))
      (if (or (vl-catch-all-error-p r) (null r)) nil T))
    T))

(defun GNI:ACTION (f a / h etype etag old new field mode elayer target e d atype atag aval alayer ok reason)
  (setq h (nth 0 a) etype (nth 1 a) etag (nth 2 a)
        old (nth 3 a) new (nth 4 a) field (nth 5 a)
        mode (nth 6 a) elayer (nth 7 a) target (nth 8 a))
  (setq e (handent h))
  (if (null e)
    (progn (GNI:ROW f (list h field "FAILED" "HANDLE_NOT_FOUND" "" "")) nil)
    (progn
      (setq d (entget e) atype (cdr (assoc 0 d)) atag (cdr (assoc 2 d))
            aval (cdr (assoc 1 d)) alayer (cdr (assoc 8 d)))
      (cond
        ((and (/= etype "") (/= (strcase etype) (strcase atype)))
          (GNI:ROW f (list h field "FAILED" "ENTITY_TYPE_CHANGED" aval alayer)) nil)
        ((and (/= etag "") (/= (strcase etag) (strcase (if atag atag ""))))
          (GNI:ROW f (list h field "FAILED" "TAG_CHANGED" aval alayer)) nil)
        ((/= (if aval aval "") (if old old ""))
          (GNI:ROW f (list h field "FAILED" "OLD_VALUE_CHANGED" aval alayer)) nil)
        ((= mode "MOVE_ONLY")
          (if (and (/= target "") (GNI:SET-LAYER e d target))
            (progn
              (GNI:ROW f (list h field "MOVED" (strcat "MOVED_TO_" target) aval target))
              T)
            (progn
              (GNI:ROW f (list h field "FAILED" "LAYER_MOVE_FAILED" aval alayer))
              nil)))
        ((= mode "VERIFY_ONLY")
          (GNI:ROW f (list h field "VERIFIED" "NO_WRITE" aval alayer)) T)
        (T
          (setq ok (GNI:SET-VALUE e d new))
          (if (not ok)
            (progn (GNI:ROW f (list h field "FAILED" "ENTMOD_FAILED" aval alayer)) nil)
            (progn
              (setq reason "UNCHANGED")
              (if (/= target "")
                (if (GNI:SET-LAYER e (entget e) target)
                  (setq reason (strcat "MOVED_TO_" target))
                  (setq reason "LAYER_MOVE_FAILED")))
              (GNI:ROW f (list h field "WRITTEN" reason new
                (cdr (assoc 8 (entget e))))) T)))))))

(defun GNI:RUN-WRITER (mode actions resultPath / f passed failed q)
  (setq passed 0 failed 0 f (open resultPath "w"))
  (if (null f)
    (princ "\nGNI_CONTROLLED_WRITER_ERROR:CANNOT_OPEN_RESULT")
    (progn
      (GNI:ROW f '("Handle" "Target Field" "Status" "Reason" "Value" "Layer"))
      (foreach a actions
        (if (GNI:ACTION f a)
          (setq passed (1+ passed))
          (setq failed (1+ failed))))
      (close f)
      (if (> failed 0)
        (princ (strcat "\nGNI_CONTROLLED_WRITER_FAILED:MODE=" mode
          ":PASSED=" (itoa passed) ":FAILED=" (itoa failed)))
        (progn
          (princ "\nGNI_CONTROLLED_WRITER_STAGE:QSAVE")
          (setq q (vl-catch-all-apply 'command-s (list "_.QSAVE")))
          (if (vl-catch-all-error-p q)
            (princ (strcat "\nGNI_CONTROLLED_WRITER_ERROR:QSAVE:"
              (vl-catch-all-error-message q)))
            (progn
              (princ (strcat "\nGNI_CONTROLLED_WRITER_OK:MODE=" mode
                ":PASSED=" (itoa passed)))
              (princ "\nGNI_WRITER_EXIT_READY")))))))
  (princ))

(princ "\nGNI_REVISION_CONTROLLED_WRITER_0_4B_2_LOADED")
(princ "\nGNI_REVISION_CONTROLLED_WRITER_0_4B_2_3_SHUTDOWN_SAFE")
(princ)
