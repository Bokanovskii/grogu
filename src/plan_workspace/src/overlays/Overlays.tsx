import { useApp } from "../state/store";
import { CommandPalette, SearchPalette, ShortcutsOverlay } from "./Palettes";
import { AskGrogu, ImpactPreview, ProposalPreview } from "./ProposalOverlays";
import { DirectivePromotion } from "./DirectivePromotion";
import { CompiledPreview } from "./CompiledPreview";
import { DeliveryLedger } from "./DeliveryLedger";
import { Migration } from "./Migration";
import { FeedbackComposerModal, NewCommentModal, RestoreQueue } from "./MiscOverlays";

// Single overlay dispatcher — at most one modal surface is open at a time.
export function Overlays() {
  const { overlay } = useApp();
  if (!overlay) return null;
  switch (overlay.kind) {
    case "commandPalette":
      return <CommandPalette />;
    case "searchPalette":
      return <SearchPalette />;
    case "shortcuts":
      return <ShortcutsOverlay />;
    case "compiledPreview":
      return <CompiledPreview />;
    case "deliveryLedger":
      return <DeliveryLedger />;
    case "askGrogu":
      return <AskGrogu threadId={overlay.threadId} />;
    case "directivePromotion":
      return <DirectivePromotion threadId={overlay.threadId} />;
    case "proposalPreview":
      return <ProposalPreview proposalId={overlay.proposalId} />;
    case "impactPreview":
      return <ImpactPreview />;
    case "migration":
      return <Migration />;
    case "feedbackComposer":
      return <FeedbackComposerModal agentKey={overlay.agentKey} nudge={overlay.nudge} />;
    case "newComment":
      return <NewCommentModal nodeId={overlay.nodeId} quote={overlay.quote} />;
    case "restoreQueue":
      return <RestoreQueue />;
    default:
      return null;
  }
}
