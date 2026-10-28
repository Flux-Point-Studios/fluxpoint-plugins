/-
Flux Point structured bridge for Lean-blaster.
Copyright 2026 Flux Point Studios. Licensed under Apache-2.0.

Uses the public API of input-output-hk/Lean-blaster (Apache-2.0).
This command returns solver evidence; it never declares a theorem or uses
blasterProven to manufacture a kernel proof. The optimizer and SMT encoding
remain trusted components of every reported result.
-/
import Lean
import Blaster

open Lean Elab Command Term Meta

namespace FluxpointBlaster

private def string (value : String) : Json := Json.str value

private def getField (value : Json) (key : String) : TermElabM Json :=
  match value.getObjVal? key with
  | .ok result => pure result
  | .error message => throwError "{message}"

private def getString (value : Json) : TermElabM String :=
  match value.getStr? with
  | .ok result => pure result
  | .error message => throwError "{message}"

private def scalarType (type : Expr) : MetaM String := do
  let type ← whnf type
  if type.isConstOf ``Nat then return "Nat"
  if type.isConstOf ``Int then return "Int"
  if type.isConstOf ``Bool then return "Bool"
  if type.isConstOf ``String then return "String"
  return (← ppExpr type).pretty

private def variables (expression : Expr) : MetaM (Array Json) :=
  forallTelescopeReducing expression fun xs _ =>
    xs.foldlM (init := #[]) fun result x => do
      let lDecl ← getFVarLocalDecl x
      if ← isProp lDecl.type then return result
      return result.push (Json.mkObj [("name", string lDecl.userName.toString),
                                     ("type", string (← scalarType lDecl.type))])

private def scalar (binding : Json) (type : Expr) : TermElabM Expr := do
  let kind ← getString (← getField binding "type")
  unless kind == (← scalarType type) do
    throwError "Replay binding type differs from the original quantified variable"
  let value ← getField binding "value"
  match kind with
  | "Nat" =>
      match value.getNat? with
      | .ok n => return mkNatLit n
      | .error message => throwError "{message}"
  | "Int" =>
      match value.getInt? with
      | .ok n => return mkIntLit n
      | .error message => throwError "{message}"
  | "Bool" =>
      match value.getBool? with
      | .ok b => return mkConst (if b then ``Bool.true else ``Bool.false)
      | .error message => throwError "{message}"
  | "String" => return mkStrLit (← getString value)
  | _ => throwError "Unsupported concrete replay type"

private partial def specializeAux (expression : Expr) (bindings : Array Json)
    (index : Nat) : TermElabM (Expr × Nat) := do
  let reduced ← whnf expression
  match reduced with
  | .forallE name type body info =>
      if ← isProp type then
        -- Preserve assumptions as implications. They are not model input
        -- assignments and the bridge must never manufacture their proofs.
        withLocalDecl name info type fun hypothesis => do
          let (body, index) ← specializeAux (body.instantiate1 hypothesis) bindings index
          return (← mkForallFVars #[hypothesis] body, index)
      else
        if index >= bindings.size then throwError "Replay is missing quantified inputs"
        let binding := bindings[index]!
        unless (← getString (← getField binding "name")) == name.toString do
          throwError "Replay binding order/name differs from the original proposition"
        specializeAux (body.instantiate1 (← scalar binding type)) bindings (index + 1)
  | _ =>
      unless index == bindings.size do
        throwError "Replay has more bindings than the original proposition"
      return (expression, index)

private def specialize (expression : Expr) (bindings : Array Json) : TermElabM Expr := do
  if bindings.isEmpty then return expression
  return (← specializeAux expression bindings 0).1

syntax (name := fluxpointCheck) "#fluxpoint_blaster " str str str str num num : command

@[command_elab fluxpointCheck]
def check : CommandElab := fun stx => do
  let nonce := (⟨stx[1]⟩ : TSyntax `str).getString
  let key := (⟨stx[2]⟩ : TSyntax `str).getString
  let declaration := ((⟨stx[3]⟩ : TSyntax `str).getString.splitOn ".").foldl Name.str Name.anonymous
  let bindingText := (⟨stx[4]⟩ : TSyntax `str).getString
  let seed := (⟨stx[5]⟩ : TSyntax `num).getNat
  let timeout := (⟨stx[6]⟩ : TSyntax `num).getNat
  liftTermElabM do
    let info ← getConstInfo declaration
    unless info.levelParams.isEmpty do
      throwError "Registered Blaster declarations must have no free universe parameters"
    let expression := match info with
      | .thmInfo theoremInfo => theoremInfo.type
      | _ => mkConst declaration
    unless (← isProp expression) do
      throwError "Registered Blaster declaration must denote a proposition"
    let parsed ← match Json.parse bindingText with
      | .ok value => pure value
      | .error message => throwError "{message}"
    let bindings ← match parsed.getArr? with
      | .ok value => pure value
      | .error message => throwError "{message}"
    let originalVariables ← variables expression
    let expression ← specialize expression bindings
    let axioms ← collectAxioms declaration
    let options : Blaster.Options.BlasterOptions :=
      { timeout := some timeout, randomSeed := some seed,
        generateCex := true, dumpSmtLib := true }
    let environment : Blaster.Optimize.TranslateEnv :=
      { (default : Blaster.Optimize.TranslateEnv) with optEnv.options.solverOptions := options }
    -- The API's expected-result diagnostics are presentation, not the machine
    -- result. Keep exceptions fatal while collecting the returned result ourselves.
    let priorMessages := (← getThe Core.State).messages
    let ((result, _), environment) ←
      Blaster.Smt.Translate.main expression (logUndetermined := false) |>.run environment
    modifyThe Core.State fun state => { state with messages := priorMessages }
    let (status, model) := match result with
      | .Valid => ("valid", #[])
      | .Falsified model => ("falsified", model.toArray.map string)
      | .Undetermined => ("undetermined", #[])
    let query := String.intercalate "\n" (environment.smtEnv.smtCommands.toList.map toString)
    let row := Json.mkObj [
      ("nonce", string nonce), ("key", string key), ("status", string status),
      ("model", Json.arr model), ("variables", Json.arr originalVariables),
      ("axioms", Json.arr (axioms.map (fun name => string name.toString))),
      ("query", string query)]
    IO.println ("FLUXPOINT_BLASTER " ++ row.compress)

end FluxpointBlaster
